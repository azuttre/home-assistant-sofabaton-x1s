/* MIT License. Created by SofaBaton X Server app. */
metadata {
    definition(name: 'SofaBaton X Hub', namespace: 'sofabaton', author: 'm3tac0de') {
        capability 'Actuator'
        capability 'Refresh'
        attribute 'availability', 'string'
        attribute 'mode', 'string'
        attribute 'controllable', 'string'
        attribute 'currentActivity', 'string'
        attribute 'currentActivityId', 'number'
        attribute 'lastCommand', 'string'
        command 'startActivity', [[name: 'Activity ID*', type: 'NUMBER']]
        command 'sendCommand', [[name: 'Entity ID*', type: 'NUMBER'], [name: 'Command ID*', type: 'NUMBER']]
        command 'allOff'
        command 'findRemote'
    }
}
void installed() { setAvailability('unknown') }
void refresh() { parent.componentCommand(device.deviceNetworkId, 'refresh') }
void startActivity(activityId) { parent.componentCommand(device.deviceNetworkId, 'startActivity', activityId) }
void sendCommand(entityId, commandId) { parent.componentCommand(device.deviceNetworkId, 'sendCommand', entityId, commandId) }
void allOff() { parent.componentCommand(device.deviceNetworkId, 'allOff') }
void findRemote() { parent.componentCommand(device.deviceNetworkId, 'findRemote') }
void setAvailability(String value) {
    sendEvent(name: 'availability', value: value)
    if (value != 'online') sendEvent(name: 'controllable', value: 'false')
}
void applyStatus(Map status) {
    sendEvent(name: 'mode', value: status.mode ?: 'disconnected')
    sendEvent(name: 'controllable', value: (status.controllable == true).toString())
}
void applyActivity(Object id, String name) {
    sendEvent(name: 'currentActivityId', value: id == null ? 0 : id)
    sendEvent(name: 'currentActivity', value: id == null ? 'Off' : (name ?: "Activity ${id}"))
}
void reportCommand(String value) { sendEvent(name: 'lastCommand', value: value, isStateChange: true) }
