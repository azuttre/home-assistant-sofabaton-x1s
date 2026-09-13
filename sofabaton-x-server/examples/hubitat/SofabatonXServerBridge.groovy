/* SofaBaton X Server transport. MIT License. Owned by the integration app. */
import groovy.json.JsonOutput

metadata {
    definition(name: 'SofaBaton X Server Bridge', namespace: 'sofabaton', author: 'm3tac0de') {
        capability 'Initialize'
        capability 'Refresh'
        attribute 'connection', 'string'
        attribute 'serverVersion', 'string'
        attribute 'lastError', 'string'
    }
}

void installed() { sendEvent(name: 'connection', value: 'unconfigured') }
void updated() { initialize() }
void initialize() { if (state.baseUrl) configureServer(state.baseUrl) }
void refresh() { parent.refreshAll() }
boolean isReady() { state.ready == true }
void configureServer(String baseUrl) {
    shutdown()
    state.baseUrl = baseUrl
    state.stopped = false
    state.retryPending = false
    state.retrySeconds = 1
    openSocket()
}
void shutdown() {
    state.stopped = true
    state.ready = false
    state.generation = (state.generation ?: 0) + 1
    unschedule()
    interfaces.webSocket.close()
}
void openSocket() {
    if (state.stopped) return
    state.ready = false
    state.generation = (state.generation ?: 0) + 1
    state.revisions = [:]
    state.hubSequences = [:]
    connection('connecting')
    try {
        String url = state.baseUrl.replaceFirst('^http', 'ws') + '/api/v1/events'
        interfaces.webSocket.connect(url, pingInterval: 30)
        runIn(20, 'checkHello')
    } catch (Exception ignored) { retryConnection('WebSocket connection failed') }
}
void checkHello() { if (!state.ready) retryConnection('Server did not send a compatible hello') }
void webSocketStatus(String message) {
    if (state.stopped) return
    if (message.startsWith('failure') || message.contains('closing') || message.contains('closed')) retryConnection('WebSocket disconnected')
}
void retryConnection(String reason) {
    if (state.stopped || state.retryPending) return
    state.ready = false
    state.generation = (state.generation ?: 0) + 1
    state.retryPending = true
    sendEvent(name: 'lastError', value: reason)
    connection('offline')
    unschedule('checkHello')
    interfaces.webSocket.close()
    Integer delay = (state.retrySeconds ?: 1) as Integer
    runIn(delay, 'reconnect')
    state.retrySeconds = Math.min(60, delay * 2)
}
void reconnect() { state.retryPending = false; openSocket() }
void connection(String value) { sendEvent(name: 'connection', value: value); parent.bridgeConnection(value) }

void parse(String raw) {
    if (state.stopped) return
    Map message
    try { message = parseJson(raw) as Map } catch (Exception ignored) {
        sendEvent(name: 'lastError', value: 'Ignored malformed WebSocket message'); return
    }
    if (message.type == 'hello') {
        if (message.api_version?.toString() != '1' || !message.instance_id) {
            shutdown()
            sendEvent(name: 'lastError', value: 'Unsupported server API; expected version 1 with instance_id')
            connection('incompatible')
            return
        }
        if (state.instanceId != message.instance_id) state.pressSequence = 0
        state.instanceId = message.instance_id
        state.ready = true
        state.retryPending = false
        state.retrySeconds = 1
        unschedule('reconnect')
        unschedule('checkHello')
        sendEvent(name: 'serverVersion', value: message.server_version)
        sendEvent(name: 'lastError', value: '')
        connection('online')
        return
    }
    if (!state.ready) return
    try {
        if (message.type == 'dropped') {
            Map revisions = state.revisions ?: [:]
            revisions.keySet().toList().each { id -> revisions[id] = (revisions[id] ?: 0) + 1 }
            state.revisions = revisions
        }
        if (message.type == 'press') {
            Long seq = message.seq as Long
            if (seq == null || seq <= ((state.pressSequence ?: 0) as Long)) return
            state.pressSequence = seq
        }
        if (message.type in ['hub_event', 'server_event']) {
            String id = message.hub_id
            Map revisions = state.revisions ?: [:]
            revisions[id] = (revisions[id] ?: 0) + 1
            state.revisions = revisions
            if (message.type == 'hub_event') {
                Map sequences = state.hubSequences ?: [:]
                Long seq = message.event.seq as Long
                if (sequences[id] != null && seq != (sequences[id] as Long) + 1) parent.reconcileSoon()
                sequences[id] = seq
                state.hubSequences = sequences
            }
        }
        parent.bridgeMessage(message)
    } catch (Exception e) {
        sendEvent(name: 'lastError', value: "Event processing failed: ${e.message}")
        parent.reconcileSoon()
    }
}

void readApi(String path, Map context) { requestApi('GET', path, null, context) }
void writeApi(String path, Map body, Map context) { requestApi('POST', path, body, context) }
void requestApi(String method, String path, Map body, Map context) {
    if (!isReady()) { parent.apiResult(context, null, 0, 'Server offline; request not sent'); return }
    Map revisions = state.revisions ?: [:]
    if (context.hub && !revisions.containsKey(context.hub)) revisions[context.hub] = 0
    state.revisions = revisions
    Map data = context + [generation: state.generation, revision: revisions[context.hub] ?: 0, method: method]
    Map params = [uri: state.baseUrl + '/api/v1' + path, timeout: 10, contentType: 'application/json']
    try {
        if (method == 'GET') asynchttpGet('httpResult', params, data)
        else {
            params.requestContentType = 'application/json'
            params.body = JsonOutput.toJson(body ?: [:])
            asynchttpPost('httpResult', params, data)
        }
    } catch (Exception ignored) {
        parent.apiResult(context, null, 0, method == 'POST' ? 'Request failed; outcome uncertain. Not retried.' : 'Request failed')
    }
}
void httpResult(response, Map context) {
    if (state.stopped || context.generation != state.generation) return
    // Do not overwrite a newer WebSocket event with an older HTTP snapshot.
    if (context.method == 'GET' && context.hub && context.revision != (state.revisions?.get(context.hub) ?: 0)) {
        if (context.kind == 'preflight') parent.apiResult(context, null, 0, 'Hub changed during status check; command not sent. Try again.')
        parent.reconcileSoon()
        return
    }
    Integer status = response.getStatus() as Integer
    Object body
    try { body = response.getJson() } catch (Exception ignored) { body = null }
    if (response.hasError() || status < 200 || status >= 300) {
        String error = body instanceof Map ? "${body.type ?: status}: ${body.detail ?: body.title ?: 'Request failed'}" : "HTTP ${status ?: 0} / connection failure"
        if (context.method == 'POST') error += '; outcome may be uncertain. Not retried.'
        parent.apiResult(context, body, status ?: 0, error)
    } else if (body == null) parent.apiResult(context, null, status, 'Server returned an invalid JSON response')
    else parent.apiResult(context, body, status, null)
}
