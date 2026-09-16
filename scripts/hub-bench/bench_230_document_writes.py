"""bench_230: whole-document writes (phase 4, P4.6) against a real hub.

The library's ``sync_hub`` end to end on a live hub, through root exports
only: a preparatory apply that creates a bench device and a bench
activity, then the plan's main document (hub rename, two device renames,
a new device with real IR payloads copied from an existing device, a new
activity using it and an existing device, a rebinding that moves a
button off a device the same document deletes, both reorders), an
unchanged document (zero writes, zero triggers), a stage B refusal after
an outside change, an ``uncertain`` item forced by dropping the answer
after a write and its resume, a cancel between items and its resume, and
finally the original document applied back so the hub is left exactly as
it was found. The engine is instrumented to count physical remote-sync
triggers on the wire (one per run that asked for one, none otherwise).

Usage (HA entry for the hub must be disabled first, single-session rule):
    .venv-py313\\Scripts\\python.exe scripts\\hub-bench\\bench_230_document_writes.py <ip> <X1|X1S|X2> <tag>

Writes out/bench_230_<tag>.json and a frame log under out/logs/.
"""

from __future__ import annotations

import asyncio
import copy
import sys
import time

import bench_common  # noqa: F401  (loads the lib under the x1slib alias)
from x1slib import (  # noqa: E402  root exports only, on purpose
    ApplyState,
    AsyncXProxy,
    ButtonName,
    DocumentError,
    HubConfig,
    PlaceholderMap,
    build_hub_sync_plan,
    edits,
)

HOST, HVER, TAG = sys.argv[1], sys.argv[2], sys.argv[3]
BENCH_MARK = "bench230"
REMOTE_SYNC_OPCODES = {0x0064, 0x0364}

REPORT: dict = {"host": HOST, "hub_version": HVER, "steps": [], "problems": [], "applies": {}}


def _t0() -> float:
    return time.monotonic()


def _row(doc: dict, kind: str, entity_id: int) -> dict:
    key = "devices" if kind == "device" else "activities"
    return next(r for r in doc[key] if r["device"]["device_id"] == entity_id)


def _binding(button: int, dev: int, cmd: int) -> dict:
    return {"button_id": int(button), "device_id": dev, "command_id": cmd,
            "long_press_device_id": None, "long_press_command_id": None}


def _names(doc: dict) -> dict:
    return {
        "hub": doc.get("hub", {}).get("name"),
        "devices": [(r["device"]["device_id"], r["device"]["name"]) for r in doc["devices"]],
        "activities": [(r["device"]["device_id"], r["device"]["name"]) for r in doc["activities"]],
    }


async def main() -> dict:
    report = REPORT

    def step(name: str, **fields) -> None:
        row = {"step": name, "t": round(_t0(), 3), **fields}
        report["steps"].append(row)
        shown = {k: v for k, v in fields.items() if k not in ("result", "plan", "doc")}
        print(f"[{name}] " + ", ".join(f"{k}={v}" for k, v in shown.items()))

    def problem(text: str) -> None:
        report["problems"].append(text)
        print("PROBLEM:", text)

    cfg = HubConfig(host=HOST, hub_version=HVER, proxy_enabled=False, source="manual")
    proxy = AsyncXProxy.from_config(cfg, diag_dump=True, diag_parse=True)

    # Instrumentation: count physical remote-sync triggers on the wire.
    engine = proxy._proxy  # noqa: SLF001  bench-only instrumentation
    counters = {"triggers": 0}
    original_enqueue = engine.enqueue_cmd

    def counting_enqueue(opcode, payload=b"", **kw):
        if int(opcode) in REMOTE_SYNC_OPCODES:
            counters["triggers"] += 1
        return original_enqueue(opcode, payload, **kw)

    engine.enqueue_cmd = counting_enqueue

    async def run_apply(label: str, baseline: dict, desired: dict, *, snapshot_id=None, state=None,
                        on_state=None, expect: str = "success"):
        records: list = []

        def keep(st):
            records.append(st.to_dict())
            if on_state is not None:
                on_state(st)

        counters["triggers"] = 0
        t = _t0()
        kwargs = {"state": state} if state is not None else {
            "baseline": baseline, "desired": desired, "snapshot_id": snapshot_id}
        try:
            result = await proxy.sync_hub(progress=lambda p: None, on_state=keep, **kwargs)
        except DocumentError as err:
            step(label, refused=err.code, text=str(err), secs=round(_t0() - t, 2))
            problem(f"{label}: stage A refused: {err.code}: {err}")
            return None, records
        secs = round(_t0() - t, 2)
        summary = {
            "status": result.status, "failed_at": result.failed_at, "message": result.message,
            "items": [(i.index, i.kind, i.entity_id if i.entity_id is not None else i.placeholder_id, i.status,
                       f"{i.completed_steps}/{i.total_steps}") for i in result.items],
            "id_map": result.id_map, "writes": result.writes, "remote_sync": result.remote_sync,
            "rebased": result.rebased, "triggers_on_wire": counters["triggers"], "secs": secs,
        }
        report["applies"][label] = {**summary, "result": result.to_dict()}
        step(label, **{k: v for k, v in summary.items() if k != "items"}, item_count=len(result.items))
        for item in summary["items"]:
            print("      ", item)
        if result.status != expect:
            problem(f"{label}: expected {expect}, got {result.status} ({result.failed_at}: {result.message})")
        return result, records

    def leftover(label: str, doc_after: dict, desired_physical: dict, created: tuple = ()) -> None:
        """Nothing should remain to do once the desired document holds.

        A created entity's client row is not hub-shaped (the hub decides the
        block), so those rows are taken from the hub, as the runner does;
        their content is checked separately by the *_check steps."""

        desired_physical = copy.deepcopy(desired_physical)
        for kind, entity_id in created:
            key = "devices" if kind == "device" else "activities"
            hub_row = next((r for r in doc_after[key] if r["device"]["device_id"] == entity_id), None)
            if hub_row is not None:
                desired_physical[key] = [copy.deepcopy(hub_row) if r["device"]["device_id"] == entity_id else r
                                         for r in desired_physical[key]]
        try:
            plan = build_hub_sync_plan(doc_after, desired_physical)
        except DocumentError as err:
            problem(f"{label}: replanning the applied document refused: {err.code}: {err}")
            return
        kinds = [(i.kind, i.entity_id if i.entity_id is not None else i.placeholder_id) for i in plan.items]
        step(f"{label}_leftover", items=kinds, step_count=plan.step_count)
        if not plan.is_empty:
            problem(f"{label}: the hub does not hold the desired document; leftover items {kinds}")
            # Diagnostics: for every leftover entity, the two rows and the step kinds.
            diag = {}
            for item in plan.items:
                if item.entity_id is None:
                    continue
                key = "devices" if item.entity_kind == "device" else "activities"
                before = next((r for r in desired_physical[key] if r["device"]["device_id"] == item.entity_id), None)
                after = next((r for r in doc_after[key] if r["device"]["device_id"] == item.entity_id), None)
                diag[f"{item.entity_kind}:{item.entity_id}"] = {
                    "steps": [(st.kind, st.label, dict(st.payload)) for st in item.steps][:6],
                    "desired_row": before, "hub_row": after,
                }
                if before and after:
                    b_bind = before.get("button_bindings") or []
                    a_bind = after.get("button_bindings") or []
                    print(f"   {item.entity_kind} {item.entity_id}: desired bindings={len(b_bind)} hub bindings={len(a_bind)} "
                          f"steps={[st.kind for st in item.steps][:8]}")
                    if b_bind and a_bind:
                        print("      desired[0]:", b_bind[0])
                        print("      hub[0]:    ", a_bind[0])
                    for key_name in sorted(set(before) | set(after)):
                        if key_name in ("captured_at", "fetched_at") or before.get(key_name) == after.get(key_name):
                            continue
                        print(f"      differs: {key_name}: {str(before.get(key_name))[:160]} | {str(after.get(key_name))[:160]}")
            report.setdefault("diagnostics", {})[label] = diag
            return False
        return True

    async with proxy:
        t_start = _t0()
        connected = await proxy.wait_connected(timeout=60)
        step("wait_connected", ok=connected, secs=round(_t0() - t_start, 2))
        if not connected:
            problem("hub never connected")
            return report
        ready = await proxy.wait_until_ready(timeout=60)
        st = await proxy.status()
        step("wait_until_ready", ok=ready, mode=st.mode, secs=round(_t0() - t_start, 2))
        if st.mode != "control":
            problem(f"expected control mode, got {st.mode}")
            return report

        # -- whole-hub read: every entity complete so any of them is editable ----
        t = _t0()
        snap0 = await proxy.refresh(progress=lambda p: None)
        incomplete = [(e.kind, e.entity_id, e.name) for e in snap0.devices + snap0.activities if not e.complete]
        step("refresh_all", secs=round(_t0() - t, 1), devices=len(snap0.devices), activities=len(snap0.activities),
             complete=snap0.complete, incomplete=incomplete)
        if any(k == "activity" for k, _i, _n in incomplete):
            problem(f"activities incomplete after a whole refresh: {incomplete}; deletions will be refused")
        D0 = copy.deepcopy(snap0.bundle)
        S0 = snap0.snapshot_id
        step("baseline", snapshot_id=S0[:12], names=_names(D0))
        stale_devices = [i for i, n in _names(D0)["devices"] if BENCH_MARK in str(n)]
        stale_activities = [i for i, n in _names(D0)["activities"] if BENCH_MARK in str(n)]
        if stale_devices or stale_activities:
            # Leftovers of an earlier run: delete them and take the baseline again.
            from x1slib import HubRejectedError  # noqa: E402

            async def _remove(what, call):
                for attempt in (1, 2):
                    try:
                        await call()
                        return
                    except HubRejectedError as err:
                        step("stale_cleanup_retry", what=what, attempt=attempt, text=str(err))
                        await asyncio.sleep(2.0)
                problem(f"could not delete the leftover {what}; aborting")
                raise RuntimeError(f"leftover {what} could not be deleted")

            for aid in stale_activities:
                await _remove(f"activity {aid}", lambda a=aid: proxy.remove_activity(a))
            for did in stale_devices:
                await _remove(f"device {did}", lambda d=did: proxy.remove_device(d))
            snap0 = await proxy.snapshot()
            D0 = copy.deepcopy(snap0.bundle)
            S0 = snap0.snapshot_id
            step("stale_cleanup", devices=stale_devices, activities=stale_activities, snapshot_id=S0[:12])

        # Source of real IR payloads: the first complete IR device with two commands.
        complete_ids = {e.entity_id for e in snap0.devices if e.complete}
        ir_dev = None
        for row in D0["devices"]:
            did = row["device"]["device_id"]
            if did in complete_ids and str(row["device"].get("device_class")) == "ir" and len(row.get("commands") or []) >= 2:
                ir_dev = row
                break
        if ir_dev is None:
            problem("no complete IR device with two commands to copy payloads from")
            return report
        ir_id = ir_dev["device"]["device_id"]
        src_cmds = ir_dev["commands"][:2]
        payloads = []
        for cmd in src_cmds:
            p = await proxy.read_payload(ir_id, cmd["command_id"])
            payloads.append(p)
        step("payload_source", device_id=ir_id, entity_name=ir_dev["device"]["name"],
             commands=[(c["command_id"], c["name"]) for c in src_cmds],
             kinds=[p.kind if p else None for p in payloads])
        if any(p is None for p in payloads):
            problem("a source command carried no payload")
            return report
        rows = [payloads[0].to_command_row(1, f"{BENCH_MARK} power"), payloads[1].to_command_row(2, f"{BENCH_MARK} mute")]
        # Two complete devices to rename, neither the payload source.
        rename_targets = [r["device"]["device_id"] for r in D0["devices"]
                          if r["device"]["device_id"] in complete_ids and r["device"]["device_id"] != ir_id][:2]
        step("rename_targets", devices=[(d, _row(D0, "device", d)["device"]["name"]) for d in rename_targets])
        if len(rename_targets) < 2:
            problem("fewer than two complete devices to rename")
            return report

        # -- P: a bench device and a bench activity to work on --------------------------
        desired = copy.deepcopy(D0)
        desired["devices"].append({"device": {"device_id": -1, "name": f"{BENCH_MARK} A", "device_class": "ir"},
                                   "commands": copy.deepcopy(rows), "button_bindings": [], "macros": [],
                                   "input_record": None, "key_sort": None})
        desired["activities"].append({"device": {"device_id": -2, "name": f"{BENCH_MARK} act", "entity_type": "activity"},
                                      "button_bindings": [_binding(ButtonName.VOL_UP, -1, 1),
                                                          _binding(ButtonName.VOL_DOWN, ir_id, src_cmds[0]["command_id"])],
                                      "favorite_slots": [], "favorites_order": [], "macros": []})
        try:
            plan = build_hub_sync_plan(D0, desired)
            step("P_plan", items=[(i.kind, i.placeholder_id or i.entity_id, i.step_count) for i in plan.items],
                 live_check=[r.to_dict() for r in plan.live_check], notes=list(plan.notes))
        except DocumentError as err:
            problem(f"P plan refused: {err.code}: {err}")
            return report
        result, _records = await run_apply("P_prepare", D0, desired, snapshot_id=S0)
        if result is None or not result.ok:
            return report
        if counters["triggers"] != 1:
            problem(f"P: expected one remote-sync trigger on the wire, saw {counters['triggers']}")
        A = result.id_map[-1]
        ACT = result.id_map[-2]
        snap1 = await proxy.snapshot()
        pm = PlaceholderMap.from_document(desired)
        pm.assign(-1, A)
        pm.assign(-2, ACT)
        if not leftover("P_prepare", snap1.bundle, pm.resolve(desired), created=(("device", A), ("activity", ACT))):
            problem("stopping before the main document: the hub already disagrees with the baseline for untouched entities")
            report["docs"] = {"D0": D0, "after_P": copy.deepcopy(snap1.bundle)}
            return report
        D1 = copy.deepcopy(snap1.bundle)
        a_row = _row(D1, "device", A)
        step("P_check", A=A, ACT=ACT, a_commands=[(c["command_id"], c["name"]) for c in a_row.get("commands") or []],
             act_bindings=_row(D1, "activity", ACT).get("button_bindings"))
        if [c["command_id"] for c in a_row.get("commands") or []] != [1, 2]:
            problem(f"bench device A commands on the hub: {a_row.get('commands')}")

        # -- M: the plan's main document -------------------------------------------------
        desired = copy.deepcopy(D1)
        original_hub_name = desired["hub"].get("name")
        desired["hub"]["name"] = f"{original_hub_name} {BENCH_MARK}"[:30]
        for did in rename_targets:
            _row(desired, "device", did)["device"]["name"] = (_row(desired, "device", did)["device"]["name"] + "*")[:30]
        desired["devices"].append({"device": {"device_id": -3, "name": f"{BENCH_MARK} B", "device_class": "ir"},
                                   "commands": copy.deepcopy(rows), "button_bindings": [], "macros": [],
                                   "input_record": None, "key_sort": None})
        desired["activities"].append({"device": {"device_id": -4, "name": f"{BENCH_MARK} act2", "entity_type": "activity"},
                                      "button_bindings": [_binding(ButtonName.VOL_UP, -3, 1),
                                                          _binding(ButtonName.VOL_DOWN, ir_id, src_cmds[0]["command_id"])],
                                      "favorite_slots": [], "favorites_order": [], "macros": []})
        act_row = _row(desired, "activity", ACT)
        act_row["button_bindings"] = [_binding(ButtonName.VOL_UP, -3, 1) if b["device_id"] == A else b
                                      for b in act_row["button_bindings"]]
        # Deleting A means removing it from every activity in the same
        # document: its power-macro steps (membership) and favorites too.
        for row in desired["activities"]:
            for macro in row.get("macros") or []:
                macro["steps"] = [s for s in macro.get("steps") or [] if s.get("device_id") != A]
            row["favorite_slots"] = [f for f in row.get("favorite_slots") or [] if f.get("device_id") != A]
            row["referenced_source_device_ids"] = [d for d in row.get("referenced_source_device_ids") or [] if d != A]
        desired["devices"] = [r for r in desired["devices"] if r["device"]["device_id"] != A]
        desired["devices"].reverse()
        desired["activities"].reverse()
        try:
            plan = build_hub_sync_plan(D1, desired)
            step("M_plan", items=[(i.kind, i.placeholder_id if i.placeholder_id is not None else i.entity_id, i.step_count)
                                  for i in plan.items],
                 live_check_count=len(plan.live_check), notes=list(plan.notes), provisional=plan.provisional_ids)
        except DocumentError as err:
            problem(f"M plan refused: {err.code}: {err}")
            return report
        result, _records = await run_apply("M_main", D1, desired, snapshot_id=snap1.snapshot_id)
        if result is None or not result.ok:
            return report
        if counters["triggers"] != 1:
            problem(f"M: expected exactly one remote-sync trigger on the wire, saw {counters['triggers']}")
        B = result.id_map[-3]
        ACT2 = result.id_map[-4]
        snap2 = await proxy.snapshot()
        pm = PlaceholderMap.from_document(desired)
        pm.assign(-3, B)
        pm.assign(-4, ACT2)
        leftover("M_main", snap2.bundle, pm.resolve(desired), created=(("device", B), ("activity", ACT2)))
        D2 = copy.deepcopy(snap2.bundle)
        step("M_check", B=B, ACT2=ACT2, names=_names(D2),
             act_bindings=_row(D2, "activity", ACT).get("button_bindings"),
             act2_bindings=_row(D2, "activity", ACT2).get("button_bindings"),
             A_gone=all(r["device"]["device_id"] != A for r in D2["devices"]))
        if any(r["device"]["device_id"] == A for r in D2["devices"]):
            problem("bench device A still on the hub after the delete")
        if _row(D2, "activity", ACT).get("button_bindings", [{}])[0].get("device_id") != B:
            problem(f"the moved binding does not point at B: {_row(D2, 'activity', ACT).get('button_bindings')}")

        # -- unchanged document: nothing to do, nothing sent ------------------------------
        result, _records = await run_apply("U_unchanged", D2, copy.deepcopy(D2), snapshot_id=snap2.snapshot_id)
        if result is not None and (result.items or counters["triggers"] or result.writes):
            problem(f"unchanged document produced items={len(result.items)} writes={result.writes} triggers={counters['triggers']}")

        # -- stage B: an outside change between the snapshot and the write ----------------
        snap3 = await proxy.snapshot()
        D3 = copy.deepcopy(snap3.bundle)
        # A binding change, not a rename: the preflight signature covers
        # bindings, macros and favorites and deliberately not the name.
        outside = edits.bind_button(D3, ACT2, ButtonName.MENU, ir_id, src_cmds[1]["command_id"])
        r = await proxy.sync_activity(baseline=D3, edited=outside, activity_id=ACT2)
        step("outside_change", ok=r.ok, failed_at=r.failed_at, completed=r.completed_steps)
        desired = copy.deepcopy(D3)
        _row(desired, "activity", ACT2)["button_bindings"] = [
            b for b in _row(desired, "activity", ACT2)["button_bindings"] if b["button_id"] != int(ButtonName.VOL_DOWN)]
        desired["hub"]["name"] = f"{original_hub_name} zz"[:30]
        result, _records = await run_apply("B_stage_b", D3, desired, snapshot_id=None, expect="stopped")
        after = await proxy.snapshot()
        hub_name_now = after.bundle.get("hub", {}).get("name")
        step("B_check", failed_at=result.failed_at if result else None, hub_name=hub_name_now,
             act2_bindings=_row(after.bundle, "activity", ACT2).get("button_bindings"))
        if result is not None and (result.failed_at != "live_check" or result.writes):
            problem(f"stage B did not stop before the first write: failed_at={result.failed_at} writes={result.writes}")
        if hub_name_now == f"{original_hub_name} zz"[:30]:
            problem("stage B failure still renamed the hub")

        # -- uncertain: the answer to a write is lost, then resume ------------------------
        snap4 = await proxy.snapshot()
        D4 = copy.deepcopy(snap4.bundle)
        desired = edits.rename_activity(D4, ACT2, f"{BENCH_MARK} act2 u")
        real_sync_activity = engine.sync_activity
        dropped = {"count": 0}

        def sync_then_drop(*args, **kwargs):
            out = real_sync_activity(*args, **kwargs)
            if dropped["count"] == 0:
                dropped["count"] += 1
                raise ConnectionResetError("bench: the answer to the write was dropped")
            return out

        engine.sync_activity = sync_then_drop
        try:
            result, records = await run_apply("Unc_uncertain", D4, desired, snapshot_id=snap4.snapshot_id, expect="stopped")
        finally:
            engine.sync_activity = real_sync_activity
        if result is not None:
            statuses = [i.status for i in result.items]
            step("Unc_check", statuses=statuses, needs_refresh=[r.to_dict() for r in result.needs_refresh],
                 resumable=result.resumable)
            if "uncertain" not in statuses or not result.resumable:
                problem(f"expected an uncertain, resumable run; got {statuses}")
            state = ApplyState.from_dict(records[-1]) if result.resumable else None
            result2 = None
            if state is not None:
                result2, _r = await run_apply("Unc_resume", D4, desired, state=state)
            if result2 is not None and result2.ok:
                snap = await proxy.snapshot()
                if _row(snap.bundle, "activity", ACT2)["device"]["name"] != f"{BENCH_MARK} act2 u":
                    problem("the resume did not leave the rename in place")
                if any(i.completed_steps for i in result2.items):
                    step("Unc_resume_note", note="the resume re-planned and wrote steps (the write had not landed)")
                else:
                    step("Unc_resume_note", note="the resume found the desired state already holding: zero steps")

        # -- cancel between items, then resume -------------------------------------------
        snap5 = await proxy.snapshot()
        D5 = copy.deepcopy(snap5.bundle)
        desired = edits.rename_activity(D5, ACT, f"{BENCH_MARK} act c")
        desired = edits.rename_activity(desired, ACT2, f"{BENCH_MARK} act2 c")
        holder: dict = {}

        def cancel_after_first(state):
            holder["last"] = state
            first_done = bool(state.items) and state.items[0].status == "done"
            if first_done and state.status == "running" and "task" in holder and not holder.get("cancelled"):
                holder["cancelled"] = True
                holder["task"].cancel()

        counters["triggers"] = 0
        task = asyncio.ensure_future(proxy.sync_hub(baseline=D5, desired=desired, snapshot_id=snap5.snapshot_id,
                                                    on_state=cancel_after_first))
        holder["task"] = task
        try:
            await task
            problem("the cancel did not propagate")
        except asyncio.CancelledError:
            pass
        state = holder.get("last")
        step("C_cancel", status=state.status if state else None,
             items=[(i.kind, i.entity_id, i.status) for i in state.items] if state else None,
             triggers_on_wire=counters["triggers"])
        if state is None or state.status != "cancelled":
            problem(f"expected a cancelled state, got {state.status if state else None}")
        else:
            report["applies"]["C_cancel"] = state.to_dict()
            resumed = ApplyState.from_dict(state.to_dict())
            result, _r = await run_apply("C_resume", D5, desired, state=resumed)
            if result is not None and result.ok:
                snap = await proxy.snapshot()
                names = {_row(snap.bundle, "activity", i)["device"]["name"] for i in (ACT, ACT2)}
                if names != {f"{BENCH_MARK} act c", f"{BENCH_MARK} act2 c"}:
                    problem(f"after the resume the activities are named {names}")

        # -- back to the original document ----------------------------------------------
        snap6 = await proxy.snapshot()
        incomplete_acts = [e.entity_id for e in snap6.activities if not e.complete]
        if incomplete_acts:
            step("refresh_incomplete_activities", ids=incomplete_acts)
            for aid in incomplete_acts:
                await proxy.refresh(activity_id=aid)
            snap6 = await proxy.snapshot()
        D6 = copy.deepcopy(snap6.bundle)
        result, _records = await run_apply("R_restore_original", D6, copy.deepcopy(D0), snapshot_id=snap6.snapshot_id)
        final = await proxy.snapshot()
        leftover("R_restore_original", final.bundle, D0)
        step("final", names=_names(final.bundle), snapshot_id=final.snapshot_id[:12], baseline_id=S0[:12],
             content_equal=final.snapshot_id == S0)
        if any(BENCH_MARK in str(n) for _i, n in _names(final.bundle)["devices"] + _names(final.bundle)["activities"]):
            problem("a bench entity is still on the hub after the restore")
        if final.bundle.get("hub", {}).get("name") != original_hub_name:
            problem(f"hub name not restored: {final.bundle.get('hub', {}).get('name')!r} vs {original_hub_name!r}")

        # An earlier stopped run left the hub's display order reversed; the
        # original was ascending ids on both tables (bench evidence, 2026-09-12).
        dev_ids = sorted(r["device"]["device_id"] for r in final.bundle["devices"])
        act_ids = sorted(r["device"]["device_id"] for r in final.bundle["activities"])
        await proxy.reorder_devices(dev_ids)
        await proxy.reorder_activities(act_ids)
        ordered = await proxy.snapshot()
        step("restore_original_order", devices=[r["device"]["device_id"] for r in ordered.bundle["devices"]],
             activities=[r["device"]["device_id"] for r in ordered.bundle["activities"]])

        st = await proxy.status()
        step("final_status", status=st.to_dict())

    return report


if __name__ == "__main__":
    import faulthandler
    import functools
    import traceback

    faulthandler.enable()
    faulthandler.dump_traceback_later(2400, exit=True)
    print = functools.partial(print, flush=True)  # noqa: A001

    bench_common.setup_logging(f"bench_230_{TAG}")
    result: dict = REPORT
    try:
        result = asyncio.run(main())
    except BaseException as err:  # noqa: BLE001
        traceback.print_exc()
        result = REPORT
        result["problems"].append(f"crashed: {type(err).__name__}: {err}")
    path = bench_common.save_json(f"bench_230_{TAG}", result)
    print("problems:", result["problems"] or "none")
    print("saved", path)
