from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, cast

from homeassistant.exceptions import PlatformNotReady
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_TOKEN
from homeassistant.helpers.typing import (
    ConfigType,
    DiscoveryInfoType,
    HomeAssistantType,
)
from homeassistant.helpers.entity_registry import async_get_registry
from homeassistant.helpers.entity import ToggleEntity
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)

from hyperion import client, const
from . import async_create_connect_hyperion_client, get_hyperion_unique_id
from .const import (
    CONF_ON_UNLOAD,
    CONF_PRIORITY,
    CONF_ROOT_CLIENT,
    DEFAULT_ORIGIN,
    DEFAULT_PRIORITY,
    DOMAIN,
    SIGNAL_INSTANCE_REMOVED,
    SIGNAL_INSTANCES_UPDATED,
    TYPE_HYPERION_SWITCH,
)

async def async_setup_platform(
    hass: HomeAssistantType,
    config: ConfigType,
    async_add_entities: Callable,
    discovery_info: Optional[DiscoveryInfoType] = None,
) -> None:
    host = config[CONF_HOST]
    port = config[CONF_PORT]
    instance = 0  # YAML only supports a single instance.

    # First, connect to the server and get the server id (which will be unique_id on a config_entry
    # if there is one).
    hyperion_client = await async_create_connect_hyperion_client(host, port)
    if not hyperion_client:
        raise PlatformNotReady
    hyperion_id = await hyperion_client.async_sysinfo_id()
    if not hyperion_id:
        raise PlatformNotReady

    return

async def async_setup_entry(
    hass: HomeAssistantType, config_entry: ConfigEntry, async_add_entities: Callable
) -> bool:
    """Set up a Hyperion platform from config entry."""
    host = config_entry.data[CONF_HOST]
    port = config_entry.data[CONF_PORT]
    token = config_entry.data.get(CONF_TOKEN)

    async def async_instances_to_entities(response: Dict[str, Any]) -> None:
        if not response or const.KEY_DATA not in response:
            return
        await async_instances_to_entities_raw(response[const.KEY_DATA])

    async def async_instances_to_entities_raw(instances: List[Dict[str, Any]]) -> None:
        registry = await async_get_registry(hass)
        entities_to_add: List[HyperionSwitch] = []
        desired_unique_ids: Set[str] = set()
        server_id = cast(str, config_entry.unique_id)

        # Add instances that are missing.
        for instance in instances:
            instance_id = instance.get(const.KEY_INSTANCE)
            if instance_id is None or not instance.get(const.KEY_RUNNING, False):
                continue

            for component in const.KEY_COMPONENTID_EXTERNAL_SOURCES + [const.KEY_COMPONENTID_LEDDEVICE]:
                unique_id = get_hyperion_unique_id(
                    server_id, instance_id, TYPE_HYPERION_SWITCH + "_" + component
                )
                desired_unique_ids.add(unique_id)
                if unique_id in current_entities:
                    continue
                hyperion_client = await async_create_connect_hyperion_client(
                    host, port, instance=instance_id, token=token
                )
                if not hyperion_client:
                    continue
                current_entities.add(unique_id)
                entities_to_add.append(
                    HyperionSwitch(
                        unique_id,
                        component,
                        config_entry.options,
                        hyperion_client,
                    )
                )

        # # Delete instances that are no longer present on this server.
        # for unique_id in current_entities - desired_unique_ids:
        #     current_entities.remove(unique_id)
        #     async_dispatcher_send(hass, SIGNAL_INSTANCE_REMOVED.format(unique_id))
        #     entity_id = registry.async_get_enty_id(SWITCH_DOMAIN, DOMAIN, unique_id)
        #     if entity_id:
        #         registry.async_remove(entity_id)

        async_add_entities(entities_to_add)

    # Readability note: This variable is kept alive in the context of the callback to
    # async_instances_to_entities below.
    current_entities: Set[str] = set()

    await async_instances_to_entities_raw(
        hass.data[DOMAIN][config_entry.entry_id][CONF_ROOT_CLIENT].instances,
    )
    hass.data[DOMAIN][config_entry.entry_id][CONF_ON_UNLOAD].append(
        async_dispatcher_connect(
            hass,
            SIGNAL_INSTANCES_UPDATED.format(config_entry.entry_id),
            async_instances_to_entities,
        )
    )
    return True

class HyperionSwitch(ToggleEntity):

    def __init__(
        self,
        unique_id: str,
        name: str,
        options: MappingProxyType[str, Any],
        hyperion_client: client.HyperionClient,
    ) -> None:
        """Initialize the light."""
        self._unique_id = unique_id
        self._name = name
        self._options = options
        self._client = hyperion_client

    @property
    def should_poll(self) -> bool:
        """Return whether or not this entity should be polled."""
        return False

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return self._name

    @property
    def available(self) -> bool:
        """Return server availability."""
        return bool(self._client.has_loaded_state)

    @property
    def unique_id(self) -> str:
        """Return a unique id for this instance."""
        return self._unique_id

    @property
    def is_on(self) -> bool:
        return  self._client.is_on([self._name])

    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        if not self.is_on:
            if not await self._client.async_send_set_component(
                **{
                    const.KEY_COMPONENTSTATE: {
                        const.KEY_COMPONENT: self._name,
                        const.KEY_STATE: True,
                    }
                }
            ):
                return

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        if not await self._client.async_send_set_component(
            **{
                const.KEY_COMPONENTSTATE: {
                    const.KEY_COMPONENT: self._name,
                    const.KEY_STATE: False,
                }
            }
        ):
            return

    def _update_components(self, _: Optional[Dict[str, Any]] = None) -> None:
        """Update Hyperion components."""
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register callbacks when entity added to hass."""
        assert self.hass
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_INSTANCE_REMOVED.format(self._unique_id),
                self.async_remove,
            )
        )

        self._client.set_callbacks(
            {
                f"{const.KEY_COMPONENTS}-{const.KEY_UPDATE}": self._update_components,
            }
        )

        # Load initial state.
        self._update_components()

    async def async_will_remove_from_hass(self) -> None:
        """Disconnect from server."""
        await self._client.async_client_disconnect()
