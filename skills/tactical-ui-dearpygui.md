# SKILL: tactical-ui-dearpygui
# Purpose: author and maintain a real-time C2 / tactical HUD (map + docks + cockpit
#          telemetry + event log) on top of Dear PyGui (DPG), driven by an RCON-fed
#          simulation. Written for an LLM agent that CAN SEE the rendered window.
#
# HOW TO USE THIS FILE (routing):
#   Section 0 = INVARIANTS. Always in effect. Never violated.
#   Sections 1-9 = load ON DEMAND by subtask. Do NOT mix concerns across sections
#   (e.g. never inline map-drawing logic into a telemetry-only edit). Pick the
#   section that matches the current subtask via the table below.
#
#   SUBTASK                                   -> READ SECTIONS
#   bootstrap app / main loop / shutdown      -> 0, 1, 2, 7
#   add or change a panel / dock-like block    -> 0, 2, 3, 4
#   draw or change the tactical map            -> 0, 2, 5
#   cockpit / high-frequency telemetry fields  -> 0, 2, 3, 6
#   wire RCON / sim events into the UI         -> 0, 2, 7
#   restyle (dark terminal look)               -> 0, 8 (+ VERIFY notes)
#   before shipping ANY ui change              -> 0, 9
#
# =============================================================================
# SECTION 0 — MANDATORY INVARIANTS (non-negotiable)
# =============================================================================
# I1. SINGLE THREAD TOUCHES GUI. Only the main render-loop thread may call any
#     dpg.* function or mutate the shared STATE object. Background threads (RCON,
#     AI, scanner) MUST NOT touch dpg or STATE directly. Their ONLY channel to the
#     UI is a thread-safe queue.Queue of plain data tuples. (This replaces Qt's
#     "signals only from GUI thread" rule, but is simpler: one queue, one drain.)
# I2. ITEMS ARE PERSISTENT, NOT PER-FRAME. Unlike C++ imgui, a DPG add_*() call
#     CREATES an item that lives until deleted. Calling add_*() every frame with
#     no guard leaks items and duplicates widgets. Therefore:
#       - create static items ONCE, before the loop (or guarded by does_item_exist);
#       - update them every frame via set_value()/configure_item() by TAG;
#       - for per-frame redraw of dynamic graphics, clear the drawing's CHILDREN
#         with delete_item(tag, children_only=True) and re-issue draw_* commands.
#         The drawing item itself is created exactly once. children_only=False on
#         a drawing deletes the pen and crashes the next frame's draw_* calls.
# I3. TAG-FIRST, NO PYTHON REFERENCES. Every interactive/updated item gets an
#     explicit string tag. Never store widget objects in variables to "keep them
#     alive"; address them by tag. This kills the Qt habit of holding pointers.
# I4. STATE IS THE SOURCE OF TRUTH; WIDGETS ARE A PROJECTION. Widgets never own
#     game/sim data. Read STATE -> project into widgets each frame. User input
#     flows back through callbacks/queue into STATE, never written "into" a widget
#     as the canonical copy.
# I5. HEAVY WORK OUT OF THE FRAME. Parsing, pathfinding, terrain scans, polygon
#     building happen in workers or between frames, producing READY-TO-DRAW data.
#     The render loop only reads prepared data and issues cheap draw/set calls.
# I6. VERIFY BEFORE SHIP. Run the Section 9 checklist. Because you can see the
#     window, after each change confirm visually AND confirm no item leak (item
#     count stable across many frames) and no traceback in the console.
#
# =============================================================================
# SECTION 1 — CANONICAL APP SKELETON (copy this shape; do not reinvent it)
# =============================================================================
# import dearpygui.dearpygui as dpg
# import queue, threading, time
#
# # --- the ONLY bridge from workers to UI (I1) ---
# MSG_Q = queue.Queue()            # holds ("topic", payload) plain-data tuples
# RUNNING = threading.Event(); RUNNING.set()
#
# def rcon_worker():               # example background producer
#     while RUNNING.is_set():
#         raw = read_rcon_line()   # blocking/polling I/O lives HERE, not in frame
#         if raw:
#             MSG_Q.put(("units", parse_units(raw)))   # data only, never dpg/STATE
#
# def drain_messages():            # called in main loop; mutates STATE safely
#     while not MSG_Q.empty():
#         topic, payload = MSG_Q.get_nowait()
#         apply_to_state(topic, payload)   # STATE mutated ONLY here (main thread)
#
# # --- bootstrap (order matters) ---
# dpg.create_context()
# dpg.create_viewport(title="C2 // Tactical Layer", width=1600, height=900,
#                     always_on_top=True)   # VERIFY overlay/transparency: Sec 10
# dpg.setup_dearpygui()
#
# build_static_ui()                # create all persistent items ONCE (I2/I3)
# init_state()                     # STATE object ready before first frame
#
# dpg.show_viewport()
# # NOTE: do NOT call dpg.start_dearpygui(); we need our OWN loop to drain the queue.
# t_last = time.perf_counter()
# while dpg.is_dearpygui_running():
#     dt = time.perf_counter() - t_last; t_last = time.perf_counter()
#     drain_messages()             # I1: fold worker data into STATE here
#     step_simulation_if_due(dt)   # I5: fixed-step sim tick outside draw cost
#     project_state_to_widgets()   # I4: STATE -> set_value/configure by tag
#     render_map()                 # Sec 5 facade (clears children, redraws)
#     dpg.render_dearpygui_frame()
#
# RUNNING.clear()                  # ask workers to stop
# dpg.destroy_context()
#
# # RULE: this skeleton is the contract. New panels/map/telemetry plug into
# # build_static_ui / project_state_to_widgets / render_map. Do not add a second
# # loop, do not move dpg calls into workers, do not create items inside the loop.
#
# =============================================================================
# SECTION 2 — STATE MODEL (single source of truth)
# =============================================================================
# Keep ONE container (dataclass or dict-of-dicts) describing the world:
#   STATE = {
#     "connection": {"host":..., "port":..., "online":False},
#     "units":   {uid: {"kind":..., "pos":(x,y), "hdg":..., "alt":..., "spd":...,
#                       "g":..., "mode":..., "ammo":{...}}},
#     "players": {name: {...}},
#     "strike_zones": {zid: {"points":[(x,y),...], "label":...}},
#     "routes":     {rid: {"points":[(x,y),...]}},
#     "log":        deque(maxlen=500),
#     "sim":        {"tick_s":0.1, "acc":0.0},
#   }
# Conventions:
#   - Tags mirror state paths for traceability, e.g. unit panel tag = f"unit_{uid}",
#     telemetry field tag = f"tel_{field}", map pen tag = "map_pen".
#   - Workers NEVER read or write STATE (I1). They enqueue; main thread applies.
#   - project_state_to_widgets() is the ONLY place that calls set_value/configure
#     for data binding; render_map() is the ONLY place that issues draw_* (Sec 5).
#   - Adding a new panel = add a slice to STATE + a builder in build_static_ui +
#     a projector line. Never bypass STATE to push text straight into a widget.
#
# =============================================================================
# SECTION 3 — WIDGET CHEATSHEET (create / configure / read / write / delete)
# =============================================================================
# All take tag=... (I3). Colors are RGBA ints 0..255. Update = set_value/configure,
# NOT re-add (I2). Common creators:
#   dpg.add_text(text, *, tag)                         # label / log line
#   dpg.add_input_text(label, *, tag, default_value, callback)
#   dpg.add_input_float(label, *, tag, default_value, callback)
#   dpg.add_button(label, *, tag, callback)
#   dpg.add_checkbox(label, *, tag, default_value, callback)
#   dpg.add_combo(label, *, tag, items=[...], default_value, callback)
#   dpg.add_slider_int(label, *, tag, min_value, max_value, default_value, callback)
#   dpg.add_drag_float(label, *, tag, speed, default_value, callback)
#   dpg.add_progress_bar(*, tag, default_value)        # value 0..1 (fuel/hull/ammo)
#   dpg.add_separator(*, tag); dpg.add_spacer(*, count)
#   dpg.add_collapsing_header(label, *, tag, default_open)   # dock-fold pattern
#   dpg.add_table(*, tag, row, column, headers=[...])  # player/unit tables
#     with dpg.table_row(): dpg.add_table_cell(); dpg.add_text(...)
#   dpg.add_plot(label, *, tag, height)                # telemetry graphs
#     with dpg.plot(): dpg.add_line_series(xs, ys, label=...)
#   dpg.add_drawing(*, tag, width, height)             # the map pen (Sec 5)
# Control plane (use these instead of recreating widgets):
#   dpg.set_value(tag, v)            # write a widget's value by tag
#   dpg.get_value(tag)               # read it back
#   dpg.configure_item(tag, **kw)    # change label/limits/show/color/etc.
#   dpg.does_item_exist(tag)         # guard for dynamic creation (I2)
#   dpg.delete_item(tag, children_only=False)   # children_only=True for pens (I2)
# Callback signature for add_*_handler / widget callback=:
#   def cb(sender, app_data, user_data): ...     # runs in MAIN thread (safe)
#   Inside cb you MAY touch dpg and STATE (you are on the GUI thread). Enqueue
#   outbound RCON commands from cb via a separate OUT_Q drained by a writer thread.
#
# =============================================================================
# SECTION 4 — PANELS / "DOCKS" PATTERN (there is no QDockWidget in DPG)
# =============================================================================
# Recreate the right-hand stack of panels as several dpg.window items with manual
# pos/size, OR as collapsing_headers/tables inside one scrollable side window.
# Recommended for a fixed dashboard: one container window per logical group,
# positioned with pos=(x,y), width/height set, no_move/no_resize as needed, and
# toggled via configure_item(win_tag, show=bool) bound to a checkbox in a toolbar.
#   with dpg.window(tag="dock_connection", label="Connection",
#                   pos=(1240, 8), width=340, no_resize=True):
#       dpg.add_text("Host:", tag="lbl_host"); ...
#   # visibility toggle, NOT destroy/recreate (I2):
#   dpg.configure_item("dock_connection", show=dpg.get_value("chk_conn"))
# Rules:
#   - Decide a layout grid ONCE (columns/rows in pixels) and store it in STATE or
#     a constant; do not let the agent recompute positions per frame.
#   - A "dock" that must scroll (event log, player list) -> put a child window or
#     a table inside; cap log length in STATE (deque) so the widget stays cheap.
#   - Persist user layout (which docks open) as booleans in STATE; rebuild shows
#     from STATE at startup, not from hardcoded visibility.
#
# =============================================================================
# SECTION 5 — MAP FACADE (the tactical layer; isolate ALL drawing here)
# =============================================================================
# The agent must NEVER scatter draw_* across the codebase. Everything map-related
# goes through these functions, which own the single pen item "map_pen" (created
# once in build_static_ui inside the map window). World->screen transform is a
# pure function of camera state (center, scale) held in STATE["map_cam"].
#
#   def w2s(wx, wy):                      # world coords -> pen-local pixels
#       cx, cy, s = STATE["map_cam"]["x"], STATE["map_cam"]["y"], STATE["map_cam"]["scale"]
#       return ((wx - cx) * s + ORIGIN_X, (wy - cy) * s + ORIGIN_Y)
#
#   def render_map():                     # called every frame from main loop
#       dpg.delete_item("map_pen", children_only=True)   # clear ONLY commands (I2)
#       for zid, z in STATE["strike_zones"].items():
#           pts = [w2s(x, y) for (x, y) in z["points"]]
#           dpg.draw_polygon(pts, color=(255,60,60,90), thickness=2, tag=f"z_{zid}")
#           dpg.draw_text(pts[0], z["label"], color=(255,120,120,255), size=13)
#       for rid, r in STATE["routes"].items():
#           pts = [w2s(x, y) for (x, y) in r["points"]]
#           for a, b in zip(pts, pts[1:]):
#               dpg.draw_line(a, b, color=(120,220,255,255), thickness=2)
#       for uid, u in STATE["units"].items():
#           p = w2s(*u["pos"])
#           col = (90,255,140,255) if u["team"]=="blue" else (255,200,80,255)
#           dpg.draw_circle(p, 6, color=col, fill=col)
#           dpg.draw_arrow(w2s(*u["pos"]), w2s(u["pos"][0]+u["hdg"]*0.01,
#                          u["pos"][1]), color=col, thickness=1)  # heading stub
#   # High-level ops the rest of the code may call (facade, not raw draw):
#   def upsert_marker(uid, **fields): STATE["units"][uid] = {...}   # data only
#   def set_strike_zone(zid, points, label): STATE["strike_zones"][zid] = {...}
#   def draw_route(rid, points): STATE["routes"][rid] = {"points": points}
#
# HIT-TEST (click on a marker / route node): use an item-handler registry bound to
# the pen, then resolve the cursor to the nearest entity in SCREEN space using the
# SAME w2s the renderer used (so hit-test and draw cannot drift apart):
#   with dpg.item_handler_registry(tag="map_hits") as reg:
#       dpg.add_item_clicked_handler(tag="map_click")     # fires on left click
#   dpg.bind_item_handler_registry("map_pen", "map_hits")
#   def on_map_click(sender, app_data, user_data):
#       # VERIFY(Sec 10): confirm cursor-space semantics before trusting offsets.
#       mx, my = dpg.get_mouse_pos(local=False)           # screen px
#       # subtract map window origin via dpg.get_item_rect_min("map_pen") if needed
#       hit = nearest_entity_in_screen_space(mx, my)      # iterate STATE, use w2s
#       if hit: STATE["selection"] = hit
#   # Keep hit-test O(n) over visible entities; if n is large, cull by viewport
#   # rect in STATE before scanning (I5).
#
# ANTI-PATTERN here: calling dpg.draw_* outside render_map(); forgetting
# children_only=True (deletes the pen -> next frame crashes); recomputing w2s
# differently in hit-test than in render (drift -> clicks miss markers).
#
# =============================================================================
# SECTION 6 — COCKPIT / TELEMETRY PATTERN (high-frequency, low-cost)
# =============================================================================
# Bottom strip (speed/alt/heading/v-speed/g/mode + fuel/hull/ammo bars + weapon
# slots) is pure projection: create each field ONCE with a tag, then every frame
# set_value/configure from STATE["units"][selected]. No re-creation (I2).
#   dpg.add_text("--", tag="tel_spd");  dpg.add_progress_bar(tag="bar_fuel")
#   # in project_state_to_widgets():
#   u = STATE["units"].get(STATE["selection"])
#   if u:
#       dpg.set_value("tel_spd", f"{u['spd']:.1f}")
#       dpg.configure_item("bar_fuel", default_value=u["fuel_frac"])  # 0..1
#       dpg.configure_item("tel_mode", label=u["mode"])               # FLYING/etc.
# Rules:
#   - Format numbers in STATE or in the projector, not in the worker.
#   - Ammo/weapon slots: a fixed row of progress bars / texts whose tags encode
#     slot index; update counts via set_value. Do NOT add/remove slot widgets per
#     frame; toggle show or set a "0/4" string instead (I2).
#   - If a value updates faster than the eye needs, throttle the set_value to the
#     sim tick (STATE["sim"]["tick_s"]) rather than every render frame (I5).
#   - Plot series (Sec 3) for altitude/speed history: keep a bounded deque in
#     STATE; rebuild the series arrays only when a new sample lands.
#
# =============================================================================
# SECTION 7 — THREADING & RCON SYNC (the Qt-race killer, made trivial)
# =============================================================================
# Topology:
#   [RCON reader thread] --MSG_Q--> [main loop: drain -> STATE -> widgets/map]
#   [main loop / cb]     --OUT_Q--> [RCON writer thread] --> server
#   [AI/scanner thread]  --MSG_Q--> (same inbound queue; tag the topic)
# Hard rules:
#   - Inbound: workers put ("topic", data) on MSG_Q. They NEVER import/use dpg
#     and NEVER touch STATE (I1). The main loop is the sole consumer.
#   - Outbound: UI callbacks (e.g. "Launch #5", "Attack", route edit) push command
#     strings/objects onto OUT_Q; a dedicated writer thread drains it to RCON.
#     This keeps blocking socket I/O off the render thread (I5).
#   - Shutdown: set RUNNING.clear() and a sentinel on queues; join workers before
#     dpg.destroy_context() so no thread calls into a torn-down context.
#   - Fixed-step sim: accumulate dt in STATE["sim"]["acc"]; while acc>=tick_s:
#     step(); acc-=tick_s. Decouples sim rate from render rate (no judder).
# ANTI-PATTERN: calling dpg.set_value from the RCON reader "because it's easier"
# (-> intermittent crashes / corrupted UI, the exact Qt disease); doing parse +
# polygon build inside render_map() (-> frame drops); sharing STATE across threads
# with a lock instead of the queue (lock-based sharing reintroduces races subtly).
#
# =============================================================================
# SECTION 8 — STYLING (dark terminal look; optional, do not block function)
# =============================================================================
# DPG supports themes via dpg.create_theme(tag) + dpg.bind_theme(theme_id). For
# the green-on-black C2 aesthetic, adjust window background / title / text /
# border colors and the viewport clear color. Exact theme color-key names vary by
# DPG version -> treat them as VERIFY (Sec 10); ship functionality first, polish
# theme second. Keep drawing colors (Sec 5) as explicit RGBA tuples in code so the
# map stays readable regardless of theme. Do not hardcode theme look into widget
# creation logic; styling is a separate pass over tags.
#
# =============================================================================
# SECTION 9 — SELF-CHECK CHECKLIST (run before declaring any UI change done)
# =============================================================================
# [ ] No dpg.* call and no STATE mutation outside the main thread (grep workers).
# [ ] Every updated widget has a stable tag; no add_*() inside the render loop
#     without a does_item_exist guard or children_only clear (I2).
# [ ] render_map() clears with children_only=True and the pen is created once.
# [ ] Hit-test uses the SAME w2s as the renderer (no drift).
# [ ] Heavy parsing/pathfinding/terrain is in workers or precomputed, not per frame.
# [ ] Queues are drained every frame; OUT_Q writer joined on shutdown.
# [ ] Visually confirmed in the live window: panels laid out, map zones/routes/
#     markers correct, telemetry ticks, log scrolls, no overlap/clipping.
# [ ] Console clean: no traceback, no "item already exists"/duplicate warnings,
#     item count stable over ~10s of runtime (leak check).
# [ ] Qt-habit scan: did I accidentally hold a widget reference, expect a signal,
#     or recreate a widget per frame? If yes -> rewrite to tag + set_value.
#
# =============================================================================
# SECTION 10 — VERIFY NOTES (platform/version caveats; check, do not assume)
# =============================================================================
# V1. Transparent always-on-top OVERLAY above the game window: DPG gives
#     always_on_top via create_viewport, but per-pixel transparent background is
#     NOT guaranteed across platforms/versions. If you need a see-through HUD over
#     Minecraft, TEST it; fallback = opaque window beside the game, or render DPG
#     to a texture composited by a thin platform window. Do not promise overlay
#     transparency in code comments until verified on the target OS.
# V2. get_mouse_pos(local=...) semantics and the exact fields of app_data passed
#     to add_item_clicked_handler differ across versions. Before relying on pixel
#     hit-tests, print/inspect them once and calibrate the screen->pen offset
#     (use the map window/pen rect). The facade in Sec 5 is written so this offset
#     lives in ONE place; fix it there, not in scattered handlers.
# V3. Theme color-key names (Sec 8) are version-dependent; confirm against the
#     installed DPG docs before binding a custom palette.
# V4. If a needed high-level map op is missing from the Sec 5 facade, ADD it to
#     the facade (and STATE) — never bypass it with a raw draw_* elsewhere. The
#     facade's completeness is part of the contract.
# =============================================================================
# END OF SKILL