/* Run with Groovy 2.4.21. No network or physical hub is used by these tests. */
import groovy.json.JsonOutput
import groovy.json.JsonSlurper

class FakeDevice {
    String deviceNetworkId
    String displayName = 'Test device'
    Map data = [:]
    Map values = [:]
    List events = []
    def getDataValue(String key) { data[key] }
    void updateDataValue(String key, String value) { data[key] = value }
    def currentValue(String key) { values[key] }
}
class FakeResponse {
    int status = 200
    Object json
    boolean error = false
    int getStatus() { status }
    Object getJson() { json }
    boolean hasError() { error }
}

File root = new File(args ? args[0] : 'sofabaton-x-server/examples/hubitat')
assert root.isDirectory()
def api = new JsonSlurper().parse(new File(root, '../../openapi.json'))
['/hubs', '/hubs/{hub_id}/status', '/hubs/{hub_id}/activities', '/hubs/{hub_id}/callback-device'].each { path ->
    assert api.paths['/api/v1' + path].get != null
}
['/activities/{activity_id}/start', '/activities/{activity_id}/stop', '/send', '/find-remote'].each { path ->
    assert api.paths['/api/v1/hubs/{hub_id}' + path].post != null
}
assert api.components.schemas.WsPress.properties.keySet().containsAll(['seq', 'hub_id', 'device_id', 'command_id', 'slot', 'press_type', 'resolution'])
List scripts = []
def makeScript = { String name, Object parent ->
    FakeDevice device = new FakeDevice(deviceNetworkId: name)
    Map timers = [:]
    List requests = []
    Map vars = [state: [:], settings: [:], device: device, parent: parent,
        app: new Expando(id: '42'), log: new Expando(warn: { Object ignored -> }),
        sendEvent: { Map event -> device.events << event; device.values[event.name] = event.value },
        runIn: { Number delay, String handler -> timers[handler] = delay },
        runEvery5Minutes: { String handler -> timers[handler] = 300 },
        unschedule: { String handler = null -> if (handler) timers.remove(handler) else timers.clear() },
        parseJson: { String raw -> new JsonSlurper().parseText(raw) },
        asynchttpGet: { String handler, Map params, Map data -> requests << [method: 'GET', handler: handler, params: params, data: data] },
        asynchttpPost: { String handler, Map params, Map data -> requests << [method: 'POST', handler: handler, params: params, data: data] }]
    List sockets = []
    vars.interfaces = [webSocket: new Expando(connect: { Map options, String url -> sockets << url }, close: { -> })]
    Script script = new GroovyShell(new Binding(vars)).parse(new File(root, name + '.groovy'))
    scripts << script
    [script: script, device: device, timers: timers, requests: requests, sockets: sockets]
}

// Compile all app/driver sources. Metadata DSL is supplied by Hubitat at installation.
Map children = [:]
def appEnv = makeScript('SofabatonXServerApp', null)
Script app = appEnv.script
app.binding.setVariable('getChildDevice', { String id -> children[id] })
app.binding.setVariable('getChildDevices', { -> children.values().toList() })
app.binding.setVariable('addChildDevice', { String ns, String type, String id, Map options ->
    Map filenames = ['SofaBaton X Server Bridge': 'SofabatonXServerBridge', 'SofaBaton X Hub': 'SofabatonXHub',
        'SofaBaton X Activity': 'SofabatonXActivity', 'SofaBaton X Buttons': 'SofabatonXButtons']
    def env = makeScript(filenames[type], app)
    env.device.deviceNetworkId = id
    env.device.displayName = options.label
    // DeviceWrapper forwards public driver methods as well as device operations.
    def wrapper = new Expando(deviceNetworkId: id, driver: env.script, env: env,
        getDataValue: { String k -> env.device.getDataValue(k) },
        updateDataValue: { String k, String v -> env.device.updateDataValue(k, v) },
        currentValue: { String k -> env.device.currentValue(k) })
    ['setAvailability', 'setLabels', 'applyStatus', 'reportCommand', 'setActive', 'configureServer'].each { method ->
        wrapper[method] = { value -> env.script.invokeMethod(method, value) }
    }
    wrapper.applyActivity = { idValue, name -> env.script.applyActivity(idValue, name) }
    wrapper.receivePress = { slot, press, label -> env.script.receivePress(slot, press, label) }
    wrapper.isReady = { -> env.script.isReady() }
    wrapper.shutdown = { -> env.script.shutdown() }
    wrapper.readApi = { path, context -> env.script.readApi(path, context) }
    wrapper.writeApi = { path, body, context -> env.script.writeApi(path, body, context) }
    children[id] = wrapper
    env.script.installed()
    wrapper
})
app.binding.setVariable('deleteChildDevice', { String id -> children.remove(id) })

String hub = 'e26a44861b45'
String hub2 = 'aabbccddeeff'
app.settings.serverUrl = 'http://192.168.1.10:8480/sofabaton/'
app.settings.selectedHubs = [hub, hub2]
app.settings.selectedActivities = ["${hub}:101".toString(), "${hub}:102".toString()]
app.settings.enableButtons = true
app.state.hubs = [(hub): [name: 'Living room', enabled: true], (hub2): [name: 'Bedroom', enabled: true]]
app.state.activities = [(hub): [[activity_id: 101, name: 'TV'], [activity_id: 102, name: 'Movie']]]
app.syncChildren(false)
app.connect()
def bridge = children[app.dni('bridge')].env
assert bridge.sockets.last() == 'ws://192.168.1.10:8480/sofabaton/api/v1/events'
bridge.script.parse(JsonOutput.toJson([type: 'hello', api_version: '1', instance_id: 'boot-1', server_version: '0.2.0', hubs: []]))
assert bridge.script.isReady()
assert bridge.requests.any { it.params.uri.endsWith('/api/v1/hubs') }

def statusBody = { Integer activity = 101, String mode = 'control' ->
    [hub_id: hub, enabled: true, status: [hub_connected: true, controllable: mode == 'control', mode: mode,
        catalog_ready: true, running_activity: activity == null ? null : [activity_id: activity, name: 'TV']]]
}
app.apiResult([kind: 'status', hub: hub], statusBody(), 200, null)
def activity101 = children[app.dni('activity', hub, '101')].env
def activity102 = children[app.dni('activity', hub, '102')].env
def hubDevice = children[app.dni('hub', hub)].env
assert activity101.device.values.switch == 'on'
assert activity102.device.values.switch == 'off'
assert hubDevice.device.values.currentActivityId == 101

// Off from an inactive dashboard tile must not stop the currently running activity.
bridge.requests.clear()
activity102.script.off()
assert bridge.requests.size() == 1 && bridge.requests[0].method == 'GET'
bridge.script.httpResult(new FakeResponse(json: statusBody()), bridge.requests[0].data)
assert bridge.requests.size() == 1

// A start sends the correct route but does not optimistically change switch state.
bridge.requests.clear()
activity102.script.on()
bridge.script.httpResult(new FakeResponse(json: statusBody()), bridge.requests[0].data)
assert bridge.requests.last().method == 'POST'
assert bridge.requests.last().params.uri.endsWith("/hubs/${hub}/activities/102/start")
assert activity102.device.values.switch == 'off'
bridge.script.httpResult(new FakeResponse(json: [accepted: true, mode: 'control']), bridge.requests.last().data)
assert hubDevice.device.values.lastCommand == 'accepted'

// Remote activity events update all activity switches, and null means observed off.
def activityEvent = { Integer seq, Integer id -> [type: 'hub_event', hub_id: hub,
    event: [seq: seq, kind: 'activity_changed', payload: [activity_id: id, name: 'Movie']]] }
bridge.script.parse(JsonOutput.toJson(activityEvent(1, 102)))
assert activity101.device.values.switch == 'off' && activity102.device.values.switch == 'on'
bridge.script.parse(JsonOutput.toJson(activityEvent(2, null)))
assert activity102.device.values.switch == 'off' && hubDevice.device.values.currentActivityId == 0

// Vendor app ownership blocks sends while preserving observed activity state.
bridge.requests.clear()
hubDevice.script.findRemote()
bridge.script.httpResult(new FakeResponse(json: statusBody(101, 'observe')), bridge.requests[0].data)
assert bridge.requests.size() == 1
assert hubDevice.device.values.availability == 'observe'
assert activity101.device.values.switch == 'on'

// Entity and command IDs stay paired in the wire request.
bridge.requests.clear()
hubDevice.script.sendCommand(7, 3)
bridge.script.httpResult(new FakeResponse(json: statusBody()), bridge.requests[0].data)
assert new JsonSlurper().parseText(bridge.requests.last().params.body) == [entity_id: 7, command_id: 3]
int sent = bridge.requests.size()
bridge.script.httpResult(new FakeResponse(status: 504, error: true, json: [type: 'hub_timeout', title: 'Timed out']), bridge.requests.last().data)
assert bridge.requests.size() == sent
assert hubDevice.device.values.lastCommand.contains('Not retried')

// allOff addresses the observed activity and does nothing when the hub is idle.
bridge.requests.clear()
hubDevice.script.allOff()
bridge.script.httpResult(new FakeResponse(json: statusBody(102)), bridge.requests[0].data)
assert bridge.requests.last().params.uri.endsWith('/activities/102/stop')
bridge.requests.clear()
hubDevice.script.allOff()
bridge.script.httpResult(new FakeResponse(json: statusBody(null)), bridge.requests[0].data)
assert bridge.requests.size() == 1

// Late HTTP state must not overwrite a newer WebSocket observation or trigger a stale command.
bridge.requests.clear()
app.refreshHub(hub)
def oldRead = bridge.requests.find { it.data.kind == 'status' }
bridge.script.parse(JsonOutput.toJson(activityEvent(3, 102)))
bridge.script.httpResult(new FakeResponse(json: statusBody()), oldRead.data)
assert activity102.device.values.switch == 'on'
bridge.requests.clear()
activity102.script.off()
def oldPreflight = bridge.requests[0]
bridge.script.parse(JsonOutput.toJson(activityEvent(4, 101)))
bridge.script.httpResult(new FakeResponse(json: statusBody(102)), oldPreflight.data)
assert bridge.requests.every { it.method == 'GET' }

// Dropped events invalidate in-flight reads even when no later event names this hub.
bridge.requests.clear()
app.refreshHub(hub)
def beforeDrop = bridge.requests.find { it.data.kind == 'status' }
bridge.script.parse(JsonOutput.toJson([type: 'dropped', count: 4]))
bridge.script.httpResult(new FakeResponse(json: statusBody(102)), beforeDrop.data)
assert activity101.device.values.switch == 'on'
assert appEnv.timers.refreshAll == 1

// Repeated distinct presses must fire, duplicates must not; another hub is isolated.
app.applyStatus(hub, statusBody())
app.saveCallback(hub, [device_id: 12, deployed: true, stale: false, labels: ['1': 'Lights', '11': 'Dim']])
def buttons = children[app.dni('buttons', hub)].env
def buttons2 = children[app.dni('buttons', hub2)].env
def press = { int seq, String type = 'short' -> [type: 'press', seq: seq, hub_id: hub, device_id: 12,
    command_id: type == 'long' ? 11 : 1, slot: 1, label: 'Lights', press_type: type, resolution: 'deployed'] }
bridge.script.parse(JsonOutput.toJson(press(1)))
bridge.script.parse(JsonOutput.toJson(press(1)))
bridge.script.parse(JsonOutput.toJson(press(2)))
bridge.script.parse(JsonOutput.toJson(press(3, 'long')))
assert buttons.device.events.count { it.name == 'pushed' } == 2
assert buttons.device.events.count { it.name == 'held' } == 1
assert buttons.device.events.findAll { it.name in ['pushed', 'held'] }.every { it.isStateChange && it.type == 'physical' }
assert !buttons2.device.events.any { it.name in ['pushed', 'held'] }
bridge.script.parse(JsonOutput.toJson(press(4) + [resolution: 'stale']))
bridge.script.parse(JsonOutput.toJson(press(5) + [device_id: 99]))
bridge.script.parse(JsonOutput.toJson(press(6) + [command_id: 20]))
assert buttons.device.events.count { it.name == 'pushed' } == 2
app.saveCallback(hub, null)
assert buttons.device.values.availability == 'callbackUnavailable'
bridge.script.parse(JsonOutput.toJson(press(7)))
assert buttons.device.events.count { it.name == 'pushed' } == 2
app.saveCallback(hub, [device_id: 12, deployed: true, stale: false, labels: ['1': 'Lights']])
buttons.script.push(2)
assert buttons.device.events.last().type == 'digital'

// Reconnect invalidates HTTP responses and preserves state instead of inventing power-off.
app.applyStatus(hub, statusBody())
bridge.requests.clear()
app.refreshHub(hub)
def previousGeneration = bridge.requests.find { it.data.kind == 'status' }
bridge.script.webSocketStatus('failure: test disconnect')
assert !bridge.script.isReady()
assert bridge.timers.reconnect == 1
assert activity101.device.values.switch == 'on'
assert activity101.device.values.availability == 'serverOffline'
bridge.script.httpResult(new FakeResponse(json: statusBody(null)), previousGeneration.data)
assert activity101.device.values.switch == 'on'
bridge.script.reconnect()
bridge.script.parse(JsonOutput.toJson([type: 'hello', api_version: '1', instance_id: 'boot-2', server_version: '0.2.0']))
bridge.script.parse(JsonOutput.toJson(press(1)))
assert buttons.device.events.count { it.name == 'pushed' && it.type == 'physical' } == 3
assert !bridge.requests.any { it.params.uri.contains('/presses') } // No stale action replay.

// Reconfiguring while a retry is pending must still allow subsequent retries.
bridge.script.webSocketStatus('failure: second disconnect')
bridge.script.configureServer(app.state.baseUrl)
bridge.script.checkHello()
assert bridge.timers.reconnect != null
bridge.script.reconnect()
bridge.script.parse(JsonOutput.toJson([type: 'hello', api_version: '1', instance_id: 'boot-2', server_version: '0.2.0']))

// Unselection is non-destructive, disabled devices cannot control the hub, cleanup is explicit.
app.settings.selectedActivities = ["${hub}:102".toString()]
app.syncChildren(false)
assert activity101.device.values.availability == 'unselected'
sent = bridge.requests.size()
activity101.script.on()
assert bridge.requests.size() == sent
app.syncChildren(true)
assert !children.containsKey(app.dni('activity', hub, '101'))

bridge.script.parse('not json')
assert bridge.device.values.lastError.contains('malformed')
bridge.script.parse(JsonOutput.toJson([type: 'hello', api_version: '2', instance_id: 'other']))
assert !bridge.script.isReady() && bridge.device.values.connection == 'incompatible'
assert scripts.collect { it.class.name }.unique().size() == 5
println 'PASS: API routes checked; app and four drivers compiled; activity control/state, command payloads, callbacks, multi-hub isolation, stale/dropped responses, reconnects and lifecycle checks passed.'
