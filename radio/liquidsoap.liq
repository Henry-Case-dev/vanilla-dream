#!/usr/bin/env liquidsoap
# Vanilla Dream Radio — Liquidsoap engine script.
#
# Install path: /etc/vanilla-dream-radio/liquidsoap.liq
# Validated on Liquidsoap 2.2.4 (Ubuntu 24.04).
#
# This script only plays what the backend prepared. It never decides the next
# track, never shuffles and never repeats: the backend owns the queue, rotation,
# schedule and manual overrides. Liquidsoap owns decoding, encoding and output.
#
# Generated includes (never committed, written on the server):
#   liquidsoap.env.liq  paths, internal API token, Icecast source password
#   encoder.liq         encoder_bitrate = <int>       (rewritten by the backend)
#   station.liq         station_name / _description / _genre (rewritten by the backend)

%include "/etc/vanilla-dream-radio/liquidsoap.env.liq"
%include "/etc/vanilla-dream-radio/encoder.liq"
%include "/etc/vanilla-dream-radio/station.liq"

# Runs as the unprivileged vdradio user, never as root.
settings.init.allow_root := false
settings.log.level := 3
settings.log.file := false

# Command server: UNIX socket only, local to the machine, never published.
settings.server.socket := true
settings.server.socket.path := "#{radio_runtime_dir}/liquidsoap.sock"

# ---------------------------------------------------------------------------
# Prepared queue. The backend writes engine-queue.txt and calls vd_sync.
#
# Correlation is done by file name, not by the `annotate:` protocol: Liquidsoap
# 2.2.4 refuses `annotate:` requests for a non-root user ("Read permission
# denied"), while on_track reports the exact path it was given. The backend
# therefore materialises one symlink per position in the runtime directory
# (<queue_link_dir>/<position_id>.mp3 -> real file) and writes those paths here.
# ---------------------------------------------------------------------------
queue = request.queue(id="radio", prefetch=2)
pending = ref([])
failed = ref([])

# A real track start is reported by the engine, not guessed from durations.
# The original engine timestamp is stored with the path so a retried event keeps
# the real start time instead of the retry time.
queue.on_track(fun (m) -> pending := list.append(pending(), [((m["filename"] ?? ""), time())]))

note_failure = fun (item) -> begin
  failed := list.append(failed(), [item])
  log("track-start post failed for #{fst(item)}")
end

post = fun (item) -> try
  let path = fst(item)
  let stamp = snd(item)
  let response = http.post(radio_internal_url,
    data="f=#{path}&ts=#{string_of(stamp)}",
    headers=[("X-Internal-Token", radio_internal_token),
             ("Content-Type", "application/x-www-form-urlencoded")],
    timeout=2.)
  # Liquidsoap returns a response record for every HTTP status. A temporary
  # failure (5xx, timeout, throttling) is retried with the original timestamp;
  # a permanent 4xx is logged and dropped instead of spinning every 0.5 s.
  let code = response.status_code
  let retry = (code >= 500) or (code == 408) or (code == 429)
  if retry then note_failure(item) end
  let permanent = (code >= 400) and (code < 500) and not retry
  if permanent then
    log("track-start rejected with #{string(code)} for #{path}")
  end
catch _err do
  note_failure(item)
end

nonempty = fun (item) -> fst(item) != ""

# One short-lived thread; a failed post is retried on the next tick.
flush = fun () -> begin
  let items = list.filter(nonempty, list.append(failed(), pending()))
  failed := []
  pending := []
  list.iter(post, items)
end

thread.run(every=0.5, fast=false, flush)

# Replace the prepared queue. Each line in the backend file is
# "<position_id>\t<absolute path>"; the reply names every request that could be
# resolved so the engine never marks an unplayable file as prepared:
#
#   OK
#   ok <position_id>
#   fail <position_id>
#   END
#
# An empty file is a valid clear ("OK" alone).
has_tab = fun (line) -> 1 < list.length(string.split(separator="\t", line))
tab_field = fun (line) -> string.split(separator="\t", line)
tab_id = fun (line) -> list.nth(default="", tab_field(line), 0)
tab_path = fun (line) -> list.nth(default="", tab_field(line), 1)

sync_apply = fun (lines) -> begin
  let clean = list.filter(has_tab, lines)
  let ids = list.map(tab_id, clean)
  let reqs = list.map(fun (line) -> request.create(tab_path(line)), clean)
  let flags = list.map(fun (request_item) -> request.resolve(request_item), reqs)
  queue.set_queue(reqs)
  let pos = ref(0)
  let mark = fun (vid) -> begin
    let idx = pos()
    pos := idx + 1
    (vid, list.nth(default=false, flags, idx))
  end
  let marked = list.map(mark, ids)
  let ok_lines = list.map(fun (video_id) -> "ok " ^ video_id,
    list.map(fun (pair) -> fst(pair), list.filter(fun (pair) -> snd(pair), marked)))
  let fail_lines = list.map(fun (video_id) -> "fail " ^ video_id,
    list.map(fun (pair) -> fst(pair), list.filter(fun (pair) -> not snd(pair), marked)))
  let reply = string.concat(separator="\n", list.append(["OK"], list.append(ok_lines, fail_lines)))
  log("vd_sync: #{string(list.length(ok_lines))} ok, #{string(list.length(fail_lines))} fail of #{string(list.length(clean))} request(s), queue length #{string(queue.length())}")
  reply
end

server.register(description="Replace the prepared queue from the backend file", "vd_sync", fun (_) ->
  begin
    let lines = if file.exists(radio_queue_file) then file.lines(radio_queue_file) else [] end
    sync_apply(lines)
  end
)

# Deterministic skip: the builtin `radio.skip` replies "Done." and the backend
# needs an unambiguous success marker.
server.register(description="Skip the current track", "vd_skip", fun (_) ->
  begin
    queue.skip()
    "OK"
  end
)

# Empty queue means technical silence on a live mount, never a broken mount and
# never a fake "on air" music state (the backend reports no_music).
radio = fallback(track_sensitive=false, [queue, blank()])

output.icecast(%mp3(bitrate=encoder_bitrate, samplerate=44100, stereo=true),
  host="127.0.0.1",
  port=8101,
  user="source",
  password=radio_icecast_source_password,
  mount=radio_mount,
  name=station_name,
  description=station_description,
  genre=station_genre,
  url=radio_public_url,
  radio)
