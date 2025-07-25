import abc
import ipaddress
import json
import logging
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Union, Optional, List, Tuple, Set, Type, Dict, Any

import networkx as nx

from wattson.cosimulation.control.messages.wattson_async_response import WattsonAsyncResponse
from wattson.cosimulation.control.messages.wattson_notification import WattsonNotification
from wattson.cosimulation.control.messages.wattson_query import WattsonQuery
from wattson.cosimulation.control.messages.wattson_response import WattsonResponse
from wattson.cosimulation.exceptions import *
from wattson.cosimulation.simulators.network.components.interface.network_entity import NetworkEntity
from wattson.cosimulation.simulators.network.components.interface.network_node import NetworkNode
from wattson.cosimulation.simulators.network.components.wattson_network_entity import WattsonNetworkEntity
from wattson.cosimulation.simulators.network.components.wattson_network_host import WattsonNetworkHost
from wattson.cosimulation.simulators.network.components.wattson_network_interface import WattsonNetworkInterface
from wattson.cosimulation.simulators.network.components.wattson_network_link import WattsonNetworkLink
from wattson.cosimulation.simulators.network.components.wattson_network_nat import WattsonNetworkNAT
from wattson.cosimulation.simulators.network.components.wattson_network_node import WattsonNetworkNode
from wattson.cosimulation.simulators.network.components.wattson_network_router import WattsonNetworkRouter
from wattson.cosimulation.simulators.network.components.wattson_network_switch import WattsonNetworkSwitch
from wattson.cosimulation.simulators.network.constants import MANAGEMENT_SWITCH, NETWORK_ENTITY, DEFAULT_SEGMENT
from wattson.cosimulation.simulators.network.messages.wattson_network_notificaction_topics import WattsonNetworkNotificationTopic
from wattson.cosimulation.simulators.network.messages.wattson_network_query import WattsonNetworkQuery
from wattson.cosimulation.simulators.network.messages.wattson_network_query_type import WattsonNetworkQueryType
from wattson.cosimulation.simulators.network.messages.wattson_network_response import WattsonNetworkResponse
from wattson.cosimulation.simulators.network.network_scenario_loader import NetworkScenarioLoader
import wattson.util
from wattson.services.configuration import ConfigurationStore, ServiceConfiguration
from wattson.cosimulation.simulators.network.wattson_segment import WattsonSegment
from wattson.cosimulation.simulators.simulator import Simulator
from wattson.services.deployment import PythonDeployment
from wattson.services.wattson_python_service import WattsonPythonService
from wattson.services.wattson_service import WattsonService
from wattson.networking.namespaces.namespace import Namespace


class NetworkEmulator(Simulator):
    def __init__(self, **kwargs):
        super().__init__()
        self.logger = wattson.util.get_logger("NetworkEmulator", "NetworkEmulator", use_context_logger=False)
        self.logger.setLevel(logging.INFO)
        self._graph = nx.Graph()
        self._config = {
            "ip_base": "172.16.0.0/16",
            "use_v6": False,
            "controller_port": 6653
        }
        self._config.update(kwargs)
        self._running: bool = False
        self._management_switch: Optional[WattsonNetworkSwitch] = None
        self._management_nat: Optional[WattsonNetworkNAT] = None
        # TODO: Load from arguments
        self._management_network = ipaddress.IPv4Network(self._config.get("management-network", "10.0.0.0/8"))
        self._management_ip_generator = self._management_network.hosts()
        self._segments = {DEFAULT_SEGMENT: WattsonSegment(name=DEFAULT_SEGMENT, server_port=55000)}

        self._switch_cls = self._config.get("switch", "wattson.networking.nodes.patched_ovs_switch.PatchedOVSSwitch")
        self._link_cls = self._config.get("link", "wattson.networking.wattson_ip_link.WattsonIPLink")
        self._controller_cls = self._config.get("controller", "wattson.networking.nodes.l2_controller.L2Controller")

    def set_configuration_store(self, configuration_store: Optional[ConfigurationStore]):
        super().set_configuration_store(configuration_store=configuration_store)
        self._fill_configuration_store()

    @abc.abstractmethod
    def cli(self):
        """
        Start a command-line-interface to interact with the network emulator
        :return:
        """
        ...

    @abc.abstractmethod
    def deploy_services(self):
        """
        Start services attached to network nodes
        :return:
        """
        ...

    def get_graph(self) -> nx.Graph:
        return self._graph

    def add_node(self, node: WattsonNetworkNode) -> WattsonNetworkNode:
        """
        Adds a WattsonNetworkNode to the network emulation
        :param node: The network node to add
        :return:
        """
        self._add_graph_node(node)
        node.network_emulator = self
        for interface in node.get_interfaces():
            if not self.has_entity(interface):
                self.add_interface(node, interface)
        self.on_topology_change(node, "add_node")
        return node

    def replace_node(self, original_node: WattsonNetworkNode, new_node: WattsonNetworkNode) -> WattsonNetworkNode:
        """
        Replaces an existing node with a new one, moving all interfaces from the original node to the new node while preserving links
        @param original_node: The node to be replaced
        @param new_node: The new node to replace the existing one.
        @return: The newly inserted node
        """
        # Delete original node (also removes edges to interfaces)
        self.remove_node(original_node, handle_interfaces=False)
        # Add new node
        self.add_node(new_node)
        # Move interface
        for interface in original_node.get_interfaces():
            # Add interface to node
            new_node.add_interface(interface)
            # Add graph edge
            self._graph.add_edge(new_node.entity_id, interface.entity_id)
        return new_node

    def add_host(self, host: WattsonNetworkHost) -> WattsonNetworkHost:
        self.add_node(host)
        self.connect_to_management_network(host)
        return host

    def add_router(self, router: WattsonNetworkRouter) -> WattsonNetworkRouter:
        self.add_host(router)
        return router

    def add_switch(self, switch: WattsonNetworkSwitch) -> WattsonNetworkSwitch:
        self.add_node(switch)
        return switch

    def get_routers(self) -> List[WattsonNetworkRouter]:
        return [node for node in self.get_nodes() if isinstance(node, WattsonNetworkRouter)]

    def has_entity(self, entity: Union[str, WattsonNetworkEntity]) -> bool:
        """
        Checks whether the given entity exists within the internal network graph.
        :param entity: The WattsonNetworkEntity or its unique entity_id
        :return: True iff the entity has a corresponding node in the graph
        """
        entity_id = entity.entity_id if isinstance(entity, WattsonNetworkEntity) else entity
        return entity_id in self._graph.nodes

    def get_entities(self) -> List[WattsonNetworkEntity]:
        return sorted([node[NETWORK_ENTITY] for node in self._graph.nodes.values()], key=lambda n: n.entity_id)

    def get_entity(self, entity: Union[str, WattsonNetworkEntity]) -> WattsonNetworkEntity:
        if isinstance(entity, WattsonNetworkEntity):
            return entity
        entity_id = entity
        if not self._graph.has_node(entity_id):
            raise NetworkNodeNotFoundException(f"Entity {entity_id} does not exist")
        entity = self._graph.nodes[entity_id][NETWORK_ENTITY]
        if not isinstance(entity, WattsonNetworkEntity):
            raise NetworkNodeNotFoundException(f"Node {entity_id} does not exist")
        return entity

    def get_nodes(self) -> List[WattsonNetworkNode]:
        return [node for node in self.get_entities()
                if isinstance(node, WattsonNetworkNode)]

    def get_node(self, node: Union[str, WattsonNetworkNode]) -> WattsonNetworkNode:
        if isinstance(node, WattsonNetworkNode):
            return node
        if isinstance(node, WattsonNetworkEntity):
            raise InvalidNetworkNodeException("The requested node is not a WattsonNetworkNode")
        node_id = WattsonNetworkNode.prefix_id(node)
        if not self._graph.has_node(node_id):
            raise NetworkNodeNotFoundException(f"Node {node_id} does not exist")
        entity = self._graph.nodes[node_id][NETWORK_ENTITY]
        if not isinstance(entity, WattsonNetworkNode):
            raise NetworkNodeNotFoundException(f"Node {node_id} does not exist")
        return entity

    def get_switch(self, node: Union[str, WattsonNetworkSwitch]) -> WattsonNetworkSwitch:
        node = self.get_node(node)
        if not isinstance(node, WattsonNetworkSwitch):
            raise NetworkNodeNotFoundException(f"Switch {node} does not exist")
        return node

    def get_switches(self) -> List[WattsonNetworkSwitch]:
        return [node for node in self.get_nodes() if isinstance(node, WattsonNetworkSwitch)]

    def get_host(self, node: Union[str, WattsonNetworkHost]) -> WattsonNetworkHost:
        node = self.get_node(node)
        if not isinstance(node, WattsonNetworkHost):
            raise NetworkNodeNotFoundException(f"Host {node} does not exist")
        return node

    def get_hosts(self) -> List[WattsonNetworkHost]:
        return [node for node in self.get_nodes() if isinstance(node, WattsonNetworkHost)]

    def get_router(self, node: Union[str, WattsonNetworkRouter]) -> WattsonNetworkRouter:
        node = self.get_node(node)
        if not isinstance(node, WattsonNetworkRouter):
            raise NetworkNodeNotFoundException(f"Router {node} does not exist")
        return node

    def get_links(self) -> List[WattsonNetworkLink]:
        return [link for link in self.get_entities()
                if isinstance(link, WattsonNetworkLink)]

    def get_interfaces(self) -> List[WattsonNetworkInterface]:
        return [interface for interface in self.get_entities()
                if isinstance(interface, WattsonNetworkInterface)]

    def add_link(self, link: WattsonNetworkLink) -> WattsonNetworkLink:
        iface_a = link.interface_a
        iface_b = link.interface_b
        iface_a.link = link
        iface_b.link = link
        link.network_emulator = self
        self._add_graph_node(link)
        self._connect_graph_nodes(iface_a, link)
        self._connect_graph_nodes(iface_b, link)
        link.add_on_link_property_change_callback(self._network_link_property_changed)
        return link

    def remove_link(self, link: Union[str, WattsonNetworkLink]):
        link = self.get_entity(link)
        self._graph.remove_node(link.entity_id)
        self.on_topology_change(link, "remove_link")
        self.on_entity_remove(link)

    def remove_switch(self, switch: Union[str, WattsonNetworkSwitch]):
        self.remove_node(switch)

    def remove_host(self, host: Union[str, WattsonNetworkHost]):
        self.remove_node(host)

    def remove_node(self, node: Union[str, WattsonNetworkNode], handle_interfaces: bool = True):
        node = self.get_node(node)
        node.stop()
        if handle_interfaces:
            for interface in node.get_interfaces():
                self.remove_interface(interface=interface)
        self._graph.remove_node(node.entity_id)
        self.on_topology_change(node, "remove_node")
        self.on_entity_remove(node)

    def remove_interface(self, interface: Union[WattsonNetworkInterface]):
        if interface.get_link() is not None:
            self.remove_link(link=interface.get_link())
        self._graph.remove_node(interface.entity_id)
        self.on_topology_change(interface, "remove_interface")
        self.on_entity_remove(interface)

    @abc.abstractmethod
    def get_namespace(self, node: Union[str, WattsonNetworkEntity], raise_exception: bool = True) -> Optional[Namespace]:
        ...

    def add_interface(self, node: Union[str, WattsonNetworkNode], interface: WattsonNetworkInterface) -> WattsonNetworkInterface:
        """
        Adds the given WattsonNetworkInterface to the specified node
        :param node: The node to add the interface to, either an ID or the node instance
        :param interface: The interface instance
        :return:
        """
        node = self.get_node(node)
        interface.network_emulator = self
        node.add_interface(interface)
        self._add_graph_node(interface)
        self._connect_graph_nodes(node, interface)
        return interface

    def has_interface(self, node: Union[str, WattsonNetworkNode], interface_id: str) -> bool:
        try:
            self.get_interface(node, interface_id)
            return True
        except InterfaceNotFoundException:
            return False
        except NetworkNodeNotFoundException:
            return False

    def get_interface(self, node: Union[str, WattsonNetworkNode], interface_id: str) -> WattsonNetworkInterface:
        node = self.get_node(node)
        for interface in node.get_interfaces():
            if interface.id == interface_id:
                return interface
        raise InterfaceNotFoundException(f"Node {node.node_id} has no interface {interface_id}")

    def connect_interfaces(self, interface_a: WattsonNetworkInterface, interface_b: WattsonNetworkInterface,
                           link_options: Optional[dict] = None) -> WattsonNetworkLink:
        if link_options is None:
            link_options = {}
        link_id = link_options.pop("id") if "id" in link_options else self.get_free_link_id()
        link = WattsonNetworkLink(id=link_id, interface_a=interface_a, interface_b=interface_b,
                                  **link_options)
        self.add_link(link)
        return link

    def connect_nodes(self, node_a: Union[str, WattsonNetworkNode], node_b: Union[str, WattsonNetworkNode],
                      interface_a_options: Optional[dict] = None,
                      interface_b_options: Optional[dict] = None,
                      link_options: Optional[dict] = None) -> Tuple[WattsonNetworkInterface, WattsonNetworkLink, WattsonNetworkInterface]:
        if interface_a_options is None:
            interface_a_options = {}
        if interface_b_options is None:
            interface_b_options = {}

        for interface_options in [interface_a_options, interface_b_options]:
            if "ip" in interface_options:
                interface_options["ip_address"] = ipaddress.IPv4Address(interface_options["ip"])
                del interface_options["ip"]
            if "mac" in interface_options:
                interface_options["mac_address"] = interface_options["mac"]
                del interface_options["mac"]
            if "prefix_length" in interface_options:
                interface_options["subnet_prefix_length"] = interface_options["prefix_length"]
                del interface_options["prefix_length"]

        interface_a = WattsonNetworkInterface(node=node_a, **interface_a_options)
        interface_b = WattsonNetworkInterface(node=node_b, **interface_b_options)
        self.add_interface(node_a, interface_a)
        self.add_interface(node_b, interface_b)
        link = self.connect_interfaces(interface_a, interface_b, link_options=link_options)
        return interface_a, link, interface_b

    def find_routers(self,
                     node: WattsonNetworkNode
                     ) -> List[Tuple[WattsonNetworkRouter, Optional[ipaddress.IPv4Network]]]:

        if isinstance(node, WattsonNetworkRouter):
            return [(node, None)]
        matches = []
        subnets = node.get_subnets()
        for router in self.get_routers():
            router_subnets = router.get_subnets()
            for subnet in subnets:
                if subnet in router_subnets:
                    matches.append((router, subnet))
        return matches

    def get_free_link_id(self) -> str:
        i = 0
        prefix = "l"
        used_ids = [link.entity_id for link in self.get_links()]
        while f"{prefix}{i}" in used_ids:
            i += 1
        return f"{prefix}{i}"

    def _add_graph_node(self, entity: WattsonNetworkEntity):
        if self._graph.has_node(entity.entity_id):
            if isinstance(entity, WattsonNetworkLink):
                raise DuplicateNetworkLinkException(f"Link {entity.entity_id} already exists")
            if isinstance(entity, WattsonNetworkNode):
                raise DuplicateNetworkNodeException(f"Node {entity.entity_id} already exists")
            if isinstance(entity, WattsonNetworkInterface):
                raise DuplicateInterfaceException(f"Interface {entity.entity_id} already exists")
            raise ValueError(f"Duplicate network entity {entity.entity_id}")
        self._graph.add_nodes_from([entity.entity_id], **{NETWORK_ENTITY: entity})

    def _connect_graph_nodes(self, entity_a: WattsonNetworkEntity, entity_b: WattsonNetworkEntity):
        if not self._graph.has_node(entity_a.entity_id):
            raise NetworkEntityNotFoundException(f"Entity {entity_a.entity_id} does not exist")
        if not self._graph.has_node(entity_b.entity_id):
            raise NetworkEntityNotFoundException(f"Entity {entity_b.entity_id} does not exist")
        if not self._graph.has_edge(entity_a.entity_id, entity_b.entity_id):
            self._graph.add_edge(entity_a.entity_id, entity_b.entity_id)

    def load_scenario(self, scenario_path: Path):
        """
        Loads the network scenario defined in the given scenario path.
        :param scenario_path: The path to the scenario configuration
        :return:
        """
        loader = NetworkScenarioLoader()
        loader.load_scenario(scenario_path=scenario_path, network_emulator=self)

    def get_primary_ips(self) -> dict:
        d = {}
        for host in self.get_hosts():
            d[host.entity_id] = host.get_primary_ip_address_string(with_subnet_length=False)
        return d

    def get_management_ips(self) -> dict:
        d = {}
        for host in self.get_hosts():
            d[host.entity_id] = host.get_management_ip_address_string(with_subnet_length=False)
        return d

    def get_next_management_ip(self) -> ipaddress.IPv4Address:
        return next(self._management_ip_generator)

    def get_all_networks(self) -> list[ipaddress.IPv4Network]:
        networks = []
        for host in self.get_hosts():
            for interface in host.get_interfaces():
                if interface.has_ip():
                    network = ipaddress.IPv4Network(interface.ip_address_string, strict=False)
                    if network not in networks:
                        networks.append(network)
        return networks

    def stop(self):
        self.logger.info("Cleaning-up service instances")
        WattsonService.clean_service_instances()

    def get_unused_ip(self, subnet: ipaddress.IPv4Network) -> ipaddress.IPv4Address:
        used_ip_addresses = []
        for interface in self.get_interfaces():
            if interface.has_ip():
                if interface.get_ip_address() in subnet:
                    used_ip_addresses.append(interface.get_ip_address())
        for ip_address in subnet.hosts():
            if ip_address not in used_ip_addresses:
                return ip_address
        raise NetworkException(f"No unused ip address in subnet {repr(subnet)} found")

    def enable_management_network(self):
        if self._management_switch is not None:
            return
        self.logger.info("Enabling management network")
        self._management_switch = WattsonNetworkSwitch(id=MANAGEMENT_SWITCH, network_emulator=self)
        self.add_switch(self._management_switch)
        for host in self.get_hosts():
            if host.get_management_ip_address_string() is not None:
                raise DuplicateInterfaceException(f"Host {host.entity_id} already has a management interface")
            self.connect_to_management_network(host=host)

    def connect_to_management_network(self, host: WattsonNetworkHost) -> bool:
        if self._management_switch is None:
            return False
        if isinstance(host, WattsonNetworkRouter):
            return True
        if host.get_management_ip_address_string() is not None:
            return True
        management_ip = self.get_next_management_ip()
        prefix_len = self._management_network.prefixlen
        self.logger.debug(f"Connecting {host.entity_id} to management network with {management_ip}")
        host_interface = WattsonNetworkInterface(id="mgm", node=host, ip_address=management_ip,
                                                 subnet_prefix_length=prefix_len, is_management=True)
        self.add_interface(host, host_interface)
        switch_interface = WattsonNetworkInterface(id=host.entity_id, node=self._management_switch, is_management=True)
        self.add_interface(self._management_switch, switch_interface)
        self.connect_interfaces(host_interface, switch_interface)
        return True

    def add_nat_to_management_network(self) -> WattsonNetworkNAT | None:
        if self._management_nat is not None:
            return self._management_nat
        nat_host = WattsonNetworkNAT(id="nat")
        nat_host.add_role("NAT")
        self.add_host(nat_host)

        if self._management_switch is None:
            self.logger.warning(f"NAT cannot be attached to management network as no management network exists")
            self._management_nat = nat_host
            return nat_host

        if self.connect_to_management_network(nat_host):
            self.logger.info(f"Created NAT {nat_host.entity_id} ({nat_host.system_name}) in management network as {nat_host.get_management_ip_address_string()}")
            self._management_nat = nat_host
            return nat_host
        else:
            self.logger.error("Could not create NAT in management network")
        return None

    def get_management_nat(self) -> WattsonNetworkNAT | None:
        return self._management_nat

    def attach_to_internet(self, host: WattsonNetworkHost) -> bool:
        nat = self.add_nat_to_management_network()
        if nat is None:
            return False
        nat.allow_traffic_from_host(host)
        nat.set_internet_route(host)

    def _fill_configuration_store(self):
        self._configuration_store.register_short_notation("ip", "primary_ip")
        self._configuration_store.register_configuration(
            "primary_ips", lambda node, store: node.network_emulator.get_primary_ips()
        )
        self._configuration_store.register_configuration(
            "management_ips", lambda node, store: node.network_emulator.get_management_ips()
        )
        self._configuration_store.register_configuration(
            "primary_ip", lambda node, store: node.get_primary_ip_address_string(with_subnet_length=False)
        )
        self._configuration_store.register_configuration(
            "management_ip", lambda node, store: node.get_management_ip_address_string(with_subnet_length=False)
        )
        self._configuration_store.register_configuration(
            "node_interfaces", lambda node, store: self.get_node_interfaces_dict(node)
        )
        self._configuration_store.register_configuration(
            "node_root_folder", lambda node, store: self.get_node_root_folder(node)
        )
        self._configuration_store.register_configuration(
            "node_root_folders", lambda _, store: {node.id: self.get_node_root_folder(node) for node in self.get_nodes()}
        )
        self._configuration_store.register_configuration(
            "artifacts_root_folder", lambda _, __: str(self.get_working_directory().absolute())
        )

    def get_node_interfaces_dict(self, node: NetworkNode) -> List[Dict]:
        interfaces = []
        for interface in node.get_interfaces():
            if not isinstance(interface, WattsonNetworkInterface):
                continue
            interfaces.append({
                "id": interface.entity_id,
                "physical_name": interface.interface_name,
                "is_management": interface.is_management,
                "mac": interface.mac_address,
                "ip": interface.ip_address_short_string,
                "subnet": interface.ip_address_string,
                "is_mirror": interface.is_mirror_port(),
                "is_physical": interface.is_physical()
            })
        return interfaces

    def get_node_root_folder(self, node: WattsonNetworkNode) -> str:
        return str(node.get_guest_folder().absolute())

    def get_simulation_control_clients(self) -> Set[str]:
        return set()

    def get_controllers(self) -> List:
        return []

    def _network_link_property_changed(self, link: WattsonNetworkLink, property_name: str, property_value: Any):
        self.send_notification(WattsonNotification(
            notification_topic=WattsonNetworkNotificationTopic.LINK_PROPERTY_CHANGED,
            notification_data={
                "link": link.entity_id,
                "property_name": property_name,
                "property_value": property_value,
                "received_ts": time.time()
            }
        ))

    """
    ####
    #### EMULATION EVENTS
    ####
    """
    def on_entity_start(self, entity: WattsonNetworkEntity):
        pass

    def on_entity_stop(self, entity: WattsonNetworkEntity):
        pass

    def on_entity_remove(self, entity: WattsonNetworkEntity):
        pass

    def on_topology_change(self, trigger_entity: WattsonNetworkEntity, change_name: str = "topology_changed"):
        pass

    def on_entity_change(self, trigger_entity: WattsonNetworkEntity, change_name: str = "entity_changed"):
        pass

    """
    ####
    #### QUERY HANDLING
    ####
    """
    def handles_simulation_query_type(self, query: Union[WattsonQuery, Type[WattsonQuery]]) -> bool:
        query_type = self.get_simulation_query_type(query)
        return issubclass(query_type, WattsonNetworkQuery)

    def handle_simulation_control_query(self, query: WattsonQuery) -> Optional[WattsonResponse]:
        if not self.handles_simulation_query_type(query):
            raise InvalidSimulationControlQueryException(f"NetworkEmulator does not handle {query.__class__.__name__}")

        if not isinstance(query, WattsonNetworkQuery):
            return None

        # Up-to-date entity representation
        if query.query_type == WattsonNetworkQueryType.GET_ENTITY:
            entity_id = query.query_data.get("entity_id")
            query.mark_as_handled()
            try:
                entity = self.get_entity(entity=entity_id)
            except NetworkNodeNotFoundException:
                return WattsonNetworkResponse(successful=False, data={"error": f"Unknown entity {entity_id=}"})
            return WattsonNetworkResponse(successful=True, data={"entity": entity.to_remote_representation()})

        if query.query_type in [WattsonNetworkQueryType.GET_NODES,
                                WattsonNetworkQueryType.ADD_NODE,
                                WattsonNetworkQueryType.REMOVE_NODE,
                                WattsonNetworkQueryType.CONNECT_NODES,
                                WattsonNetworkQueryType.NODE_ACTION,
                                WattsonNetworkQueryType.UPDATE_NODE_CONFIGURATION]:
            return self._handle_node_simulation_control_query(query)

        if query.query_type in [WattsonNetworkQueryType.GET_SERVICE,
                                WattsonNetworkQueryType.GET_SERVICES,
                                WattsonNetworkQueryType.ADD_SERVICE,
                                WattsonNetworkQueryType.SERVICE_ACTION]:
            return self._handle_service_simulation_control_query(query)

        if query.query_type in [WattsonNetworkQueryType.GET_LINKS,
                                WattsonNetworkQueryType.SET_LINK_PROPERTY,
                                WattsonNetworkQueryType.SET_LINK_UP,
                                WattsonNetworkQueryType.SET_LINK_DOWN,
                                WattsonNetworkQueryType.GET_LINK_STATE,
                                WattsonNetworkQueryType.REMOVE_LINK]:
            return self._handle_link_simulation_control_query(query)

        if query.query_type in [WattsonNetworkQueryType.SET_INTERFACE_IP]:
            return self._handle_interface_simulation_control_query(query)

        if query.query_type == WattsonNetworkQueryType.GET_UNUSED_IP:
            query.mark_as_handled()
            subnet = query.query_data.get("subnet")
            try:
                ip_address = self.get_unused_ip(subnet)
            except NetworkException as e:
                return WattsonNetworkResponse(successful=False, data={"error": f"{e=}"})
            return WattsonNetworkResponse(successful=True, data={"ip_address": ip_address})

    def _get_remote_representations(self, entities: List[WattsonNetworkEntity], force: bool = True) -> Dict:
        representations = {
            entity.entity_id: entity.to_remote_representation(force_state_synchronization=force) for entity in entities
        }
        return representations

    """
    QUERY HANDLING: LINK-RELATED QUERIES
    """
    def _handle_link_simulation_control_query(self, query: WattsonQuery) -> Optional[WattsonResponse]:
        link = None
        if query.query_data.get("entity_id") is not None:
            try:
                link = self.get_entity(entity=query.query_data.get("entity_id"))
            except NetworkNodeNotFoundException as e:
                self.logger.error(traceback.print_exception(*sys.exc_info()))
                return WattsonNetworkResponse(successful=False, data={"error": f"{e=}"})
            if not isinstance(link, WattsonNetworkLink):
                return WattsonNetworkResponse(successful=False, data={"error": f"Requested entity is not a link"})

        if query.query_type == WattsonNetworkQueryType.GET_LINKS:
            query.mark_as_handled()
            links = self.get_links()
            response = WattsonNetworkResponse(
                True,
                data={
                    "links": self._get_remote_representations(links, force=False)
                }
            )
            return response

        if query.query_type == WattsonNetworkQueryType.GET_LINK_STATE:
            query.mark_as_handled()
            state = link.get_link_state()
            return WattsonNetworkResponse(
                successful=True,
                data={
                    "link_state": state
                }
            )

        if query.query_type in [WattsonNetworkQueryType.SET_LINK_UP,
                                WattsonNetworkQueryType.SET_LINK_DOWN]:
            query.mark_as_handled()
            if query.query_type == WattsonNetworkQueryType.SET_LINK_DOWN:
                link.down()
            elif query.query_type == WattsonNetworkQueryType.SET_LINK_UP:
                link.up()
            return WattsonNetworkResponse(successful=True)

        if query.query_type == WattsonNetworkQueryType.REMOVE_LINK:
            query.mark_as_handled()
            self.remove_link(link)
            return WattsonNetworkResponse(successful=True)

        if query.query_type == WattsonNetworkQueryType.SET_LINK_PROPERTY:
            query.mark_as_handled()
            property_name = query.query_data.get("property_name")
            property_value = query.query_data.get("property_value")
            link_model = link.get_link_model()
            property_map = {
                "delay": "set_delay_from_timespan",
                "jitter": "set_jitter_from_timespan",
                "packet_loss": "set_packet_loss_from_string",
                "bandwidth": "set_bandwidth_from_string"
            }
            if property_name not in property_map.keys():
                return WattsonNetworkResponse(successful=False, data={"error": f"Property {property_name} not supported"})
            try:
                method = getattr(link_model, property_map[property_name])
                method(property_value)
                return WattsonNetworkResponse(successful=True)
            except Exception as e:
                return WattsonNetworkResponse(successful=False, data={"error": f"Could not set {property_name} = {property_value}. {e=}"})
        return None

    """
    QUERY HANDLING: NODE-RELATED QUERIES
    """
    def _handle_node_simulation_control_query(self, query: WattsonQuery) -> Optional[WattsonResponse]:
        # Listing of nodes
        if query.query_type == WattsonNetworkQueryType.GET_NODES:
            query.mark_as_handled()
            nodes = self.get_nodes()
            response = WattsonNetworkResponse(
                True, data={
                    "nodes": self._get_remote_representations(nodes)
                }
            )
            return response

        if query.query_type == WattsonNetworkQueryType.ADD_NODE:
            query.mark_as_handled()
            entity_id = str(query.query_data.get("entity_id", ""))
            node_type: str = query.query_data.get("node_type")
            arguments: Optional[dict] = query.query_data.get("arguments")
            config: Optional[dict] = query.query_data.get("config")

            if self.has_entity(entity_id):
                return WattsonNetworkResponse(successful=False, data={"error": f"Node with {entity_id=} already exists"})

            node_type_name = node_type

            if node_type_name == "NetworkHost":
                node = WattsonNetworkHost(id=entity_id, *arguments, config=config)
                self.add_host(node)
            elif node_type_name == "NetworkRouter":
                node = WattsonNetworkRouter(id=entity_id, *arguments, config=config)
                self.add_router(node)
            elif node_type_name == "NetworkSwitch":
                node = WattsonNetworkSwitch(id=entity_id, *arguments, config=config)
                self.add_switch(node)
            else:
                return WattsonNetworkResponse(successful=False, data={"error": f"Invalid {node_type_name=}"})
            return WattsonNetworkResponse(successful=True, data={"entity_id": node.entity_id})

        if query.query_type == WattsonNetworkQueryType.CONNECT_NODES:
            query.mark_as_handled()
            node_a_id = str(query.query_data.get("entity_id_a", ""))
            node_b_id = str(query.query_data.get("entity_id_b", ""))
            update_default_routes = query.query_data.get("update_default_routes", True)

            node_a = self.get_node(node_a_id)
            node_b = self.get_node(node_b_id)
            if node_a is None:
                return WattsonNetworkResponse(successful=False, data={"error": f"Node {node_a_id=} not found"})
            if node_b is None:
                return WattsonNetworkResponse(successful=False, data={"error": f"Node {node_b_id=} not found"})
            link_options = query.query_data.get("link_options")
            interface_a_options = query.query_data.get("interface_a_options")
            interface_b_options = query.query_data.get("interface_b_options")
            interface_a, link, interface_b = self.connect_nodes(
                node_a,
                node_b,
                interface_a_options=interface_a_options,
                interface_b_options=interface_b_options,
                link_options=link_options
            )
            self.on_topology_change(link, "nodes_connected")
            if update_default_routes:
                if isinstance(node_a, WattsonNetworkHost):
                    node_a.update_default_route()
                if isinstance(node_b, WattsonNetworkHost):
                    node_b.update_default_route()

            return WattsonNetworkResponse(successful=True, data={
                "interface_a": interface_a.entity_id,
                "interface_b": interface_b.entity_id,
                "link": link.entity_id
            })

        if query.query_type == WattsonNetworkQueryType.REMOVE_NODE:
            entity_id = query.query_data.get("entity_id")
            query.mark_as_handled()
            try:
                node = self.get_node(node=entity_id)
            except NetworkNodeNotFoundException:
                return WattsonNetworkResponse(successful=False, data={"error": f"Unknown node {entity_id=}"})
            self.remove_node(node)
            self.on_topology_change(node, "node_removed")
            return WattsonNetworkResponse(successful=True)

        # Node start, stop and restart handling
        if query.query_type == WattsonNetworkQueryType.NODE_ACTION:
            action = query.query_data.get("action")
            entity_id = query.query_data.get("entity_id")
            query.mark_as_handled()
            if action not in ["start", "stop", "start_pcap", "stop_pcap", "open_terminal", "exec",
                              "add-role", "delete-role", "loopback_up", "update_default_route",
                              "start-browser"]:
                return WattsonNetworkResponse(successful=False, data={"error": f"Unsupported action {action=}"})
            try:
                node = self.get_node(node=entity_id)
            except NetworkNodeNotFoundException:
                return WattsonNetworkResponse(successful=False, data={"error": f"Unknown node {entity_id=}"})
            if action == "add-role":
                node.add_role(query.query_data["role"])
                return WattsonNetworkResponse(successful=True)
            if action == "delete-role":
                node.delete_role(query.query_data["role"])
                return WattsonNetworkResponse(successful=True)
            if action == "exec":
                ret, data = node.exec(query.query_data["value"])
                return WattsonNetworkResponse(successful=True, data={"code": ret, "lines": data})
            if action == "start":
                node.start()
                self.on_topology_change(node, "node_start")
                return WattsonNetworkResponse(successful=True)
            if action == "stop":
                node.stop()
                self.on_topology_change(node, "node_stop")
                return WattsonNetworkResponse(successful=True)
            if action == "start_pcap" or action == "stop_pcap":
                interface_id = query.query_data.get("interface")
                interface = None
                if interface_id is not None:
                    interface = node.get_interface(interface_id)
                    if interface is None:
                        return WattsonNetworkResponse(successful=False, data={"error": f"Unknown interface {interface_id}"})
                if action == "start_pcap":
                    services = node.start_pcap(interface=interface)
                    return WattsonNetworkResponse(successful=True, data={"services": [service.id for service in services]})
                else:
                    node.stop_pcap(interface=interface)
                    return WattsonNetworkResponse(successful=True)
            if action == "open_terminal":
                if node.open_terminal():
                    return WattsonNetworkResponse(successful=True)
                return WattsonNetworkResponse(successful=False, data={"error": "Could not open terminal"})
            if action == "loopback_up":
                if not isinstance(node, WattsonNetworkHost):
                    return WattsonNetworkResponse(successful=False, data={"error": "Only hosts have loopback interfaces"})
                if node.loopback_up():
                    return WattsonNetworkResponse(successful=True)
                return WattsonNetworkResponse(successful=False, data={"error": "Could not bring loopback interface up"})
            if action == "update_default_route":
                if not isinstance(node, WattsonNetworkHost):
                    return WattsonNetworkResponse(successful=False, data={"error": "Only hosts can update their default route"})
                if node.update_default_route():
                    return WattsonNetworkResponse(successful=True)
                return WattsonNetworkResponse(successful=False, data={"error": "Could not update default route"})
            if action == "start-browser":
                if self.open_browser(node):
                    return WattsonNetworkResponse(successful=True)
                return WattsonNetworkResponse(successful=False, data={"error": "Cannot open browser for this node"})

        if query.query_type == WattsonNetworkQueryType.UPDATE_NODE_CONFIGURATION:
            config = query.query_data["config"]
            entity_id = query.query_data["entity_id"]
            try:
                node = self.get_node(entity_id)
            except NetworkNodeNotFoundException:
                return WattsonNetworkResponse(successful=False, data={"error": "Node not found"})
            node.update_config(config)
            return WattsonNetworkResponse(successful=True, data={"config": node.get_config()})
        return None

    """
    QUERY HANDLING: INTERFACE-RELATED QUERIES
    """
    def _handle_interface_simulation_control_query(self, query: WattsonQuery) -> Optional[WattsonResponse]:
        if query.query_type == WattsonNetworkQueryType.SET_INTERFACE_IP:
            query.mark_as_handled()
            try:
                interface = self.get_entity(query.query_data.get("entity_id"))
            except NetworkEntityNotFoundException:
                return WattsonNetworkResponse(successful=False, data={"error": "Interface not found"})
            if not isinstance(interface, WattsonNetworkInterface):
                return WattsonNetworkResponse(successful=False, data={"error": "Interface not found"})
            ip = query.query_data.get("ip_address")
            interface.set_ip_address(ip_address=ip)
            return WattsonNetworkResponse(successful=True)

    """
    QUERY HANDLING: SERVICE-RELATED QUERIES
    """
    def _handle_service_simulation_control_query(self, query: WattsonQuery) -> Optional[WattsonResponse]:
        # Get (all) services
        if query.query_type == WattsonNetworkQueryType.GET_SERVICES:
            nodes = self.get_nodes()
            services = []
            for node in nodes:
                for service_id, service in node.get_services().items():
                    services.append(service.to_remote_representation())
            return WattsonNetworkResponse(successful=True, data={"services": services})

        # Get a single service
        if query.query_type == WattsonNetworkQueryType.GET_SERVICE:
            service_id = query.query_data.get("service_id")
            try:
                service = WattsonService.get_instance(service_id=service_id)
            except ServiceException:
                return WattsonNetworkResponse(successful=False, data={"error": f"Service {service_id=} not found"})
            return WattsonNetworkResponse(successful=True, data={"service": service.to_remote_representation()})

        # Service action (async!)
        if query.query_type == WattsonNetworkQueryType.SERVICE_ACTION:
            service_id = query.query_data.get("service_id")
            action = query.query_data.get("action")
            parameters = query.query_data.get("params")
            try:
                service = WattsonService.get_instance(service_id=service_id)
            except ServiceException:
                return WattsonNetworkResponse(successful=False, data={"error": f"Service {service_id=} not found"})

            def perform_action(_service: WattsonService, _action: str, _params: dict, _async_response: WattsonAsyncResponse):
                if not hasattr(_service, _action):
                    _async_response.resolve(WattsonNetworkResponse(successful=False, data={"error": f"Action {action} is not available"}))
                method = getattr(_service, action)
                _response = WattsonNetworkResponse(successful=False)
                try:
                    result = method(**_params)
                    success = True
                    if isinstance(result, bool):
                        success = result
                    _response = WattsonNetworkResponse(
                        successful=success,
                        data={"result": result, "service": _service.to_remote_representation()}
                    )
                except Exception as e:
                    self.logger.error(f"{e=}")
                    self.logger.error(traceback.print_exc(*sys.exc_info()))
                    _response = WattsonNetworkResponse(successful=False, data={"error": repr(e)})
                finally:
                    _async_response.resolve(_response)

            query.mark_as_handled()
            async_response = WattsonAsyncResponse()
            t = threading.Thread(target=perform_action, args=(service, action, parameters, async_response))
            t.start()
            return async_response

        # Add a new service to a node
        if query.query_type == WattsonNetworkQueryType.ADD_SERVICE:
            query.mark_as_handled()
            entity_id = query.query_data.get("entity_id")
            service_configuration: ServiceConfiguration = query.query_data.get("configuration")
            service_type = query.query_data.get("service_type")
            if service_type != "python":
                return WattsonNetworkResponse(successful=False, data={"error": f"Unsupported {service_type=}, expected 'python'"})
            deployment_class_path = query.query_data.get("deployment_class")
            try:
                node = self.get_node(node=entity_id)
            except NetworkNodeNotFoundException:
                return WattsonNetworkResponse(successful=False, data={"error": f"Unknown node {entity_id=}"})

            try:
                deployment_class = wattson.util.dynamic_load_class(deployment_class_path)
                if not issubclass(deployment_class, PythonDeployment):
                    return WattsonNetworkResponse(successful=False, data={"error": f"Requested deployment class is not a PythonDeployment"})
            except RuntimeError:
                return WattsonNetworkResponse(successful=False, data={"error": f"Requested deployment class could not be loaded"})
            service = WattsonPythonService(service_class=deployment_class, service_configuration=service_configuration, network_node=node)
            service.ensure_working_directory()
            node.add_service(service)
            return WattsonNetworkResponse(successful=True, data={"service": service.to_remote_representation()})
        return None

    """
    Utility and helper functions
    """
    def open_browser(self, node: WattsonNetworkNode) -> bool:
        """
        Open a (local) browser for this node if possible
        @param node: The node to open a browser for
        @return: Whether a browser could be opened
        """
        return False
