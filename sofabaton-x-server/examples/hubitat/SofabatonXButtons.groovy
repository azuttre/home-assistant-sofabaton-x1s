/* MIT License. Button numbers correspond to server callback slots 1..10. */
import groovy.json.JsonOutput

metadata {
    definition(name: 'SofaBaton X Buttons', namespace: 'sofabaton', author: 'm3tac0de') {
        capability 'PushableButton'
        capability 'HoldableButton'
        capability 'Refresh'
        attribute 'availability', 'string'
        attribute 'buttonLabels', 'string'
        attribute 'lastButtonLabel', 'string'
    }
}
void installed() { sendEvent(name: 'numberOfButtons', value: 10); setAvailability('unknown') }
void updated() { sendEvent(name: 'numberOfButtons', value: 10) }
void refresh() { parent.componentCommand(device.deviceNetworkId, 'refresh') }
void setAvailability(String value) { sendEvent(name: 'availability', value: value) }
void setLabels(Map labels) { sendEvent(name: 'buttonLabels', value: JsonOutput.toJson(labels)) }
void receivePress(Integer slot, String pressType, String label) {
    emitButton(slot, pressType == 'long' ? 'held' : 'pushed', 'physical', label)
}
// Capability commands simulate Hubitat button events; they do not transmit to SofaBaton.
void push(button) { emitButton(button as Integer, 'pushed', 'digital', '') }
void hold(button) { emitButton(button as Integer, 'held', 'digital', '') }
void emitButton(Integer slot, String event, String source, String label) {
    if (slot == null || slot < 1 || slot > 10) return
    sendEvent(name: 'lastButtonLabel', value: label ?: "Button ${slot}")
    sendEvent(name: event, value: slot, type: source, isStateChange: true,
        descriptionText: "${device.displayName} button ${slot} ${event}")
}
