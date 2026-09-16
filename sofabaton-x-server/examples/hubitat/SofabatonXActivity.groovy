/* MIT License. Switch state is the hub's reported activity, not equipment telemetry. */
metadata {
    definition(name: 'SofaBaton X Activity', namespace: 'sofabaton', author: 'm3tac0de') {
        capability 'Switch'
        capability 'Refresh'
        attribute 'availability', 'string'
    }
}
void installed() { setAvailability('unknown') }
void on() { parent.componentCommand(device.deviceNetworkId, 'on') }
void off() { parent.componentCommand(device.deviceNetworkId, 'off') }
void refresh() { parent.componentCommand(device.deviceNetworkId, 'refresh') }
void setActive(boolean active) { sendEvent(name: 'switch', value: active ? 'on' : 'off') }
void setAvailability(String value) { sendEvent(name: 'availability', value: value) }
