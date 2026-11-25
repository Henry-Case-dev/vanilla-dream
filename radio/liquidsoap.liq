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
queue.on_track(fun (m) -> pending := list.append(pending(), [(m["filename"] ?? "")]))

note_failure = fun (path) -> begin
  failed := list.append(failed(), [path])
  log("track-start post failed for #{path}")
end

post = fun (path) -> try
  ignore(http.post(radio_internal_url,
    data="f=#{path}&ts=#{string_of(time())}",
    headers=[("X-Internal-Token", radio_internal_token),
             ("Content-Type", "application/x-www-form-urlencoded")],
    timeout=2.))
catch _err do
  note_failure(path)
end

nonempty = fun (path) -> path != ""

# One short-lived thread; a failed post is retried on the next tick.
flush = fun () -> begin
  let paths = list.filter(nonempty, list.append(failed(), pending()))
  failed := []
  pending := []
  list.iter(post, paths)
end

thread.run(every=0.5, fast=false, flush)

# Replace the prepared queue. The reply reports how many requests Liquidsoap
# could actually resolve, so the backend never marks an unplayable file as
# prepared: "OK <resolved> <failed>".
sync_counts = fun (flags) -> begin
  let ok = list.length(list.filter(fun (flag) -> flag, flags))
  "OK #{string(ok)} #{string(list.length(flags) - ok)}"
end

sync_apply = fun (lines) -> begin
  let reqs = list.map(fun (line) -> request.create(line), lines)
  let flags = list.map(fun (request_item) -> request.resolve(request_item), reqs)
  queue.set_queue(reqs)
  log("vd_sync: #{sync_counts(flags)} of #{string(list.length(reqs))} request(s), queue length #{string(queue.length())}")
  sync_counts(flags)
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
