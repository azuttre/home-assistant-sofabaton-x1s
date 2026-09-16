/* SofaBaton X Server - Hubitat example. MIT License. See README.md. */
definition(name: "SofaBaton X Server", namespace: "sofabaton", author: "m3tac0de",
    description: "Local activity control and remote buttons through sofabaton-x-server.",
    category: "Convenience", singleInstance: false, iconUrl: "", iconX2Url: "")

preferences { page(name: "mainPage") }

def mainPage() {
    dynamicPage(name: "mainPage", title: "SofaBaton X Server", install: true, uninstall: true) {
        section("1. Connect to the server") {
            input "serverUrl", "text", title: "Server base URL, e.g. http://192.168.1.10:8480",
                required: true, submitOnChange: true
            paragraph "Run the Python server on a separate LAN host. Enter its URL without /api/v1. Use one app installation per server."
            input "connectServer", "button", title: "Connect / reload hubs"
            paragraph "Connection: ${bridge()?.currentValue('connection') ?: 'not configured'}"
            if (state.notice) paragraph escapeText(state.notice)
        }
        section("2. Choose hubs and load their activities") {
            Map options = (state.hubs ?: [:]).collectEntries { id, h ->
                [(id): "${h.name} (${id})"]
            }
            input "selectedHubs", "enum", title: "Hubs", options: options, multiple: true,
                required: false, submitOnChange: true
            paragraph "Only hubs with a stable MAC ID appear. Register hubs on the server first and wait for the initial connection."
            input "loadCatalogs", "button", title: "Load selected hubs"
        }
        section("3. Expose activities and buttons") {
            Map options = [:]
            selected().each { id ->
                (state.activities?.get(id) ?: []).each { a ->
                    options["${id}:${a.activity_id}".toString()] = "${hubName(id)} / ${a.name}"
                }
            }
            input "selectedActivities", "enum", title: "Activity switches", options: options,
                multiple: true, required: false, submitOnChange: true
            input "enableButtons", "bool", title: "Create a ten-button remote for each selected hub",
                defaultValue: true
            input "applyDevices", "button", title: "Apply device selection"
            paragraph "Remote buttons use existing server callback slots: short = pushed, long = held. Follow the example README to deploy and assign callbacks. Ordinary IR/Bluetooth presses are not reported."
            selected().each { id ->
                def cb = state.callbacks?.get(id)
                paragraph escapeText("${hubName(id)}: " + (cb?.deployed && !cb?.stale ?
                    "callback device ${cb.device_id}; " + (cb.labels ?: [:]).collect { k, v -> "${k}: ${v}" }.join(', ') :
                    "callback device unavailable or not configured"))
            }
            paragraph "Deselecting retains existing devices and their last state, but marks them unavailable. Use the explicit cleanup button below to delete them after updating your rules."
            input "removeUnused", "button", title: "Delete deselected devices"
        }
    }
}

String escapeText(Object value) {
    value.toString().replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
}
List selected() { (settings.selectedHubs ?: []).collect { it.toString() } }
List activitySelection() { (settings.selectedActivities ?: []).collect { it.toString() } }
String hubName(String id) { state.hubs?.get(id)?.name ?: id }
String dni(String kind, String hub = '', String entity = '') { "sofabaton:${app.id}:${kind}:${hub}:${entity}" }
def bridge() { getChildDevice(dni('bridge')) }
def hubDevice(String id) { getChildDevice(dni('hub', id)) }

void installed() { initialize() }
void updated() { initialize() }
void initialize() {
    unschedule()
    if (settings.serverUrl) connect()
    syncChildren(false)
    // These are cheap server-state reads; never run a full structural refresh on a timer.
    runEvery5Minutes("refreshAll")
}
void uninstalled() { bridge()?.shutdown() }
void appButtonHandler(String name) {
    switch (name) {
        case 'connectServer': connect(); break
        case 'loadCatalogs': syncChildren(false); refreshAll(); break
        case 'applyDevices': syncChildren(false); refreshAll(); break
        case 'removeUnused': syncChildren(true); break
    }
}
void connect() {
    try {
        String base = settings.serverUrl?.trim()?.replaceAll('/+$', '')
        def uri = new java.net.URI(base ?: '')
        if (!(uri.scheme in ['http', 'https']) || !uri.host || uri.userInfo || uri.query || uri.fragment || base.endsWith('/api/v1')) {
            throw new IllegalArgumentException('Enter an http(s) server base URL without credentials, query, fragment or /api/v1.')
        }
        if (state.baseUrl && state.baseUrl != base && (state.hubs || selected())) {
            throw new IllegalArgumentException('Use a new app installation for a different server URL; this preserves existing device identities.')
        }
        state.baseUrl = base
        if (!bridge()) addChildDevice('sofabaton', 'SofaBaton X Server Bridge', dni('bridge'),
            [label: 'SofaBaton X Server Bridge', isComponent: true])
        bridge().configureServer(base)
        state.notice = 'Connecting. Reopen this page after a few seconds to see the hub list.'
    } catch (Exception e) { state.notice = e.message; log.warn "SofaBaton setup: ${e.message}" }
}

void syncChildren(boolean removeUnused) {
    Set wanted = [dni('bridge')] as Set
    selected().each { id ->
        ensureDevice('hub', id, '', 'SofaBaton X Hub', hubName(id), wanted)
        if (settings.enableButtons != false) ensureDevice('buttons', id, '', 'SofaBaton X Buttons', "${hubName(id)} Remote", wanted)
        activitySelection().findAll { it.startsWith(id + ':') }.each { key ->
            String aid = key.split(':')[1]
            def a = (state.activities?.get(id) ?: []).find { it.activity_id.toString() == aid }
            ensureDevice('activity', id, aid, 'SofaBaton X Activity', "${hubName(id)} / ${a?.name ?: aid}", wanted)
        }
    }
    getChildDevices().findAll { !wanted.contains(it.deviceNetworkId) }.each { child ->
        if (removeUnused) deleteChildDevice(child.deviceNetworkId)
        else child.setAvailability('unselected')
    }
}
void ensureDevice(String kind, String hub, String entity, String driver, String label, Set wanted) {
    String id = dni(kind, hub, entity)
    wanted.add(id)
    def child = getChildDevice(id)
    if (!child) child = addChildDevice('sofabaton', driver, id, [label: label, isComponent: false])
    child.updateDataValue('kind', kind)
    child.updateDataValue('hubId', hub)
    if (entity) child.updateDataValue('activityId', entity)
}

void refreshAll() {
    if (!bridge()?.isReady()) return
    bridge().readApi('/hubs', [kind: 'hubs'])
    selected().each { refreshHub(it) }
}
void refreshHub(String id) {
    if (!selected().contains(id) || !bridge()?.isReady()) return
    bridge().readApi("/hubs/${id}/status", [kind: 'status', hub: id])
    bridge().readApi("/hubs/${id}/callback-device", [kind: 'callback', hub: id])
    if (state.hubs?.get(id)?.enabled != false) bridge().readApi("/hubs/${id}/activities", [kind: 'activities', hub: id])
}
void reconcileSoon() { runIn(1, 'refreshAll') }
void bridgeConnection(String value) {
    if (value != 'online') selected().each { id -> setAvailability(id, 'serverOffline') }
    else refreshAll()
}
void setAvailability(String id, String value) {
    getChildDevices().findAll { it.getDataValue('hubId') == id }.each { child ->
        String kind = child.getDataValue('kind')
        boolean enabled = selected().contains(id) && (kind != 'activity' || activitySelection().contains("${id}:${child.getDataValue('activityId')}".toString())) &&
            (kind != 'buttons' || settings.enableButtons != false)
        String availability = enabled ? value : 'unselected'
        if (enabled && value in ['online', 'observe']) {
            if (kind == 'buttons' && (!state.callbacks?.get(id)?.deployed || state.callbacks?.get(id)?.stale)) availability = 'callbackUnavailable'
            if (kind == 'activity' && state.activities?.containsKey(id) &&
                !(state.activities[id].any { it.activity_id.toString() == child.getDataValue('activityId') })) availability = 'removed'
        }
        child.setAvailability(availability)
    }
}

// Called only by the bridge. Response bodies are already parsed, with errors separated.
void apiResult(Map context, Object body, Integer status, String error) {
    String id = context.hub
    if (id && !selected().contains(id)) return
    if (error) {
        if (context.kind == 'callback' && status == 404) {
            saveCallback(id, null)
            return
        }
        String message = "${context.kind}: ${error}"
        state.notice = message
        if (id) hubDevice(id)?.reportCommand(message)
        if (context.kind == 'status' || context.kind == 'preflight') setAvailability(id, 'unavailable')
        log.warn "SofaBaton ${message}"
        return
    }
    switch (context.kind) {
        case 'hubs':
            Map hubs = [:]
            body.findAll { it.hub_id ==~ /[0-9a-f]{12}/ }.each { h ->
                hubs[h.hub_id] = [name: h.config?.name ?: h.hub_id, enabled: h.enabled]
            }
            state.hubs = hubs
            state.notice = "Found ${hubs.size()} hub(s). Choose hubs, load activities, then apply your device selection."
            selected().findAll { !hubs.containsKey(it) }.each { setAvailability(it, 'removed') }
            break
        case 'status': applyStatus(id, body); break
        case 'preflight':
            applyStatus(id, body)
            executeControl(context, body)
            break
        case 'activities':
            Map catalogs = state.activities ?: [:]
            catalogs[id] = body.collect { [activity_id: it.activity_id, name: it.name] }
            state.activities = catalogs
            setAvailability(id, hubDevice(id)?.currentValue('availability') ?: 'unknown')
            break
        case 'callback': saveCallback(id, body); break
        case 'control':
            hubDevice(id)?.reportCommand(body?.accepted == true ? 'accepted' : 'not accepted')
            reconcileSoon()
            break
    }
}
void saveCallback(String id, Object body) {
    Map callbacks = state.callbacks ?: [:]
    callbacks[id] = body ? [device_id: body.device_id, deployed: body.deployed, stale: body.stale, labels: body.labels] : [:]
    state.callbacks = callbacks
    getChildDevice(dni('buttons', id))?.setLabels(body?.labels ?: [:])
    setAvailability(id, hubDevice(id)?.currentValue('availability') ?: 'unknown')
}
void applyStatus(String id, Map record) {
    Map status = record.status ?: [:]
    String availability = !record.enabled ? 'disabled' : !status.hub_connected ? 'offline' : status.mode == 'observe' ? 'observe' : 'online'
    setAvailability(id, availability)
    hubDevice(id)?.applyStatus(status)
    // Offline/disabled is unknown, not an observed power-off.
    if (record.enabled && status.hub_connected) applyActivity(id, status.running_activity?.activity_id, status.running_activity?.name)
}
void applyActivity(String id, Object activityId, String name) {
    hubDevice(id)?.applyActivity(activityId, name)
    getChildDevices().findAll { it.getDataValue('hubId') == id && it.getDataValue('kind') == 'activity' }.each { child ->
        if (activitySelection().contains("${id}:${child.getDataValue('activityId')}".toString())) {
            child.setActive(activityId != null && child.getDataValue('activityId') == activityId.toString())
        }
    }
}

void bridgeMessage(Map message) {
    String id = message.hub_id
    if (message.type == 'dropped') { state.notice = 'Events were dropped; reconciling state. Missed button actions are not replayed.'; reconcileSoon(); return }
    if (message.type == 'server_event') {
        if (message.kind in ['callback_device_stale', 'callback_device_restored']) saveCallback(id, null)
        if (selected().contains(id) && message.kind in ['hub_removed', 'hub_disabled']) setAvailability(id, message.kind)
        reconcileSoon()
        return
    }
    if (!selected().contains(id)) return
    if (message.type == 'hub_event') {
        Map event = message.event
        if (event.kind == 'activity_changed') applyActivity(id, event.payload.activity_id, event.payload.name)
        // Event payloads contain partial state; obtain complete status from the cheap status endpoint.
        if (event.kind in ['activity_changed', 'hub_state', 'app_state', 'status_changed', 'catalog_ready', 'activity_list_updated', 'snapshot_changed', 'ota']) reconcileSoon()
    } else if (message.type == 'press' && settings.enableButtons != false) {
        def cb = state.callbacks?.get(id)
        if (!cb?.deployed || cb?.stale || message.resolution != 'deployed' || cb.device_id != message.device_id) return
        Integer slot = message.slot as Integer
        if (slot == null || slot < 1 || slot > 10 || !(message.press_type in ['short', 'long'])) return
        Integer expected = slot + (message.press_type == 'long' ? 10 : 0)
        if (message.command_id != expected) return
        getChildDevice(dni('buttons', id))?.receivePress(slot, message.press_type, message.label)
    }
}

// All commands take a fresh status snapshot. Never retry a control POST after a timeout.
void componentCommand(String childId, String command, Object first = null, Object second = null) {
    def child = getChildDevice(childId)
    String id = child?.getDataValue('hubId')
    if (!id || !selected().contains(id)) return
    String kind = child.getDataValue('kind')
    if (kind == 'activity' && !activitySelection().contains("${id}:${child.getDataValue('activityId')}".toString())) return
    if (command == 'refresh') { refreshHub(id); return }
    if (!bridge()?.isReady()) { hubDevice(id)?.reportCommand('Server is offline; command not sent'); return }
    Map context = [kind: 'preflight', hub: id, command: command]
    try {
        if (command in ['on', 'off']) context.activity = child.getDataValue('activityId').toInteger()
        else if (command == 'startActivity') context.activity = first.toString().toInteger()
        else if (command == 'sendCommand') {
            context.entity = first.toString().toInteger()
            context.code = second.toString().toInteger()
            if (context.entity < 1 || context.entity > 255 || context.code < 0) throw new IllegalArgumentException('Invalid command IDs')
        } else if (!(command in ['allOff', 'findRemote'])) return
        if (context.activity != null && (context.activity < 101 || context.activity > 255)) throw new IllegalArgumentException('Activity ID must be 101..255')
    } catch (Exception ignored) { hubDevice(id)?.reportCommand('Invalid IDs; use IDs from the server catalog'); return }
    hubDevice(id)?.reportCommand('checking status')
    bridge().readApi("/hubs/${id}/status", context)
}
void executeControl(Map context, Map record) {
    String id = context.hub
    Map status = record.status ?: [:]
    if (!record.enabled || !status.controllable || !status.catalog_ready) {
        hubDevice(id)?.reportCommand("Control unavailable (${status.mode ?: 'disabled'})")
        return
    }
    Object running = status.running_activity?.activity_id
    String path
    Map payload = [:]
    switch (context.command) {
        case 'on': case 'startActivity':
            if (running == context.activity) { hubDevice(id)?.reportCommand('already active'); return }
            path = "/activities/${context.activity}/start"; break
        case 'off': case 'allOff':
            if (running == null || (context.command == 'off' && running != context.activity)) {
                hubDevice(id)?.reportCommand('requested activity is already off'); return
            }
            path = "/activities/${running}/stop"; break
        case 'sendCommand': path = '/send'; payload = [entity_id: context.entity, command_id: context.code]; break
        case 'findRemote': path = '/find-remote'; break
        default: return
    }
    bridge().writeApi("/hubs/${id}${path}", payload, [kind: 'control', hub: id])
}
