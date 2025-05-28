#!/usr/bin/env python3
from __future__ import annotations

import copy
import datetime
from pathlib import Path
from typing import Dict, Tuple, Optional, cast
import zipfile

import gpxpy
from gpxpy.gpx import GPX, GPXTrack, GPXTrackSegment
from geopy.distance import distance


def _hms(td: datetime.timedelta) -> str:
    """Format a timedelta as “Hh Mm Ss”, omitting zero fields."""

    total = int(round(td.total_seconds()))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h}h")
    if h or m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _print_pause_summary(stats: dict, *, tz_offset: int = 0) -> None:
    """Human-readable reporting of pause-trimming operation.

    Args:
        stats: dict returned by ``_trim_track`` / ``run_pause_trimmer``.
        tz_offset: Kept for backward compatibility but no longer used.
    """

    if "activity_start" in stats:
        t0 = stats["activity_start"]
    elif stats["pauses"]:
        t0 = min(p["start"] for p in stats["pauses"])
    else:  # no pauses found
        t0 = None

    print(f"Activity date  {stats['activity_start']:%Y-%m-%d}")
    print(f"Start time  {stats['activity_start']:%H:%M:%S} UTC")
    print(" ")
    print(f"{'Pause':>5}  {'Relative time':>15}  {'Duration':>12}  " f"{'Removed':>12}  {'Drift':>9}")

    for i, p in enumerate(stats["pauses"], 1):
        # Format Δt as HH:MM:SS (zero-padded, hours may exceed 24)
        if t0 is not None:
            rel_td = p["start"] - t0
            total = int(round(rel_td.total_seconds()))
            hh, rem = divmod(total, 3600)
            mm, ss = divmod(rem, 60)
            rel = f"{hh:02d}:{mm:02d}:{ss:02d}"
        else:
            rel = "—"

        gap = _hms(p["gap"])
        cut = _hms(p["removed"])
        drift = f"{round(p['drift']):>3}m"

        print(f"{i:>5}  {rel:>15}  {gap:>12}  {cut:>12}  {drift:>9}")

    print(" ")
    print(f"Original elapsed time {_hms(stats['orig_elapsed']):>12}")
    print(f"Trimmed elapsed time {_hms(stats['trimmed_elapsed']):>12}")
    print(f"Total pause time {_hms(stats['removed_time']):>12}")
    print("-" * 55)


def _decode_name(info: zipfile.ZipInfo) -> str:
    """Return a proper string for *info.filename*.

    Some zips lack the UTF-8 flag; repair their names.
    """
    # bit 11 set → filename is already UTF-8
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode("cp437")  # undo zipfile’s default decoding
    try:
        return raw.decode("utf-8")  # most likely correct
    except UnicodeDecodeError:
        return raw.decode("latin-1")  # graceful fallback


def _ts(t: Optional[datetime.datetime]) -> datetime.datetime:
    """Return *t* if it’s a datetime, else raise."""
    if t is None:
        raise ValueError("GPX point is missing its <time> stamp")
    return cast(datetime.datetime, t)


def _trim_track(original: GPX, *, min_speed: float = 0.5, min_pause_duration: int = 600) -> Tuple[str, Dict]:
    """
    Trim long, low-speed pauses from a GPX track and shift all subsequent
    timestamps backward so that *elapsed time equals true moving time*.

    Args:
        original: The parsed GPX object to trim.
        min_speed: Speed threshold, in m s⁻¹, below which motion is considered
            stationary.
        min_pause_duration: Minimum pause length, in seconds, that must be sustained
            before it is removed.

    Returns:
        xml: The trimmed GPX, serialized as a single XML string with all
            namespace information preserved.
        stats: A dictionary containing
            * ``pauses``        – list of removed-pause dicts
            * ``removed_time``  – total pause time as ``timedelta``
            * ``pause_drift``  – total “drift” distance in metres
            * ``cum_shift``     – cumulative timestamp shift
            * ``orig_elapsed``  – elapsed time before trimming
            * ``trimmed_elapsed`` – elapsed time after trimming

    Notes:
        soft pause: *Sequence of points within ONE segment* whose instantaneous speed
            drops below `min_speed`. We keep just enough of the pause to cover the drift
            distance at the *average moving speed so far* and cut the rest.
        hard pause: Gap between two consecutive segments. Same rule: we keep the minimum time
            needed to traverse the straight- line distance at average speed and trim the excess.
        Returned `stats["pauses"]` rows therefore show gap ≥ removed ≥ 0  for **both** pause kinds.
    """
    # ── boilerplate: deep-copy and bookkeeping ──────────────────────────────
    trimmed = copy.deepcopy(original)
    trimmed.tracks = []  # we rebuild tracks from scratch

    stats = {  # aggregate totals + pause list
        "pauses": [],  # list[dict]
        "removed_time": datetime.timedelta(),
        "pause_drift": 0.0,
        "cum_shift": datetime.timedelta(),
    }

    # helper: clone → time-shift → append, keeping timestamps strictly monotonic
    def _append(dst_seg, src_pt, *, shift: datetime.timedelta, last_time: datetime.datetime | None):
        new = copy.deepcopy(src_pt)
        new.time -= shift  # apply global time shift

        # GPX consumers need monotonically increasing timestamps
        if last_time and new.time <= last_time:
            new.time = last_time + datetime.timedelta(milliseconds=1)
        dst_seg.points.append(new)
        return new.time  # → becomes the next last_time

    cum_shift = datetime.timedelta()  # total time removed so far

    # ────────────────────────── iterate over tracks & segments ──────────────
    for src_trk in original.tracks:
        dst_trk = GPXTrack()
        dst_trk.name = src_trk.name
        dst_trk.type = src_trk.type
        trimmed.tracks.append(dst_trk)

        moving_dist = moving_time = 0.0  # for average-speed estimates

        for seg_idx, src_seg in enumerate(src_trk.segments):
            dst_seg = GPXTrackSegment()
            dst_trk.segments.append(dst_seg)
            if not src_seg.points:
                continue

            last_written = _append(dst_seg, src_seg.points[0], shift=cum_shift, last_time=None)

            # state for a potential soft pause
            p_start_idx = None  # first low-speed pt index
            p_start_time = None  # timestamp of preceding pt
            p_drift = 0.0  # metres drifted during pause

            pts = src_seg.points
            for i in range(1, len(pts)):
                prev, curr = pts[i - 1], pts[i]
                dt = (_ts(curr.time) - _ts(prev.time)).total_seconds()
                if dt <= 0:  # duplicate / rewind in source
                    last_written = _append(dst_seg, curr, shift=cum_shift, last_time=last_written)
                    continue

                # instantaneous speed between two source points
                d_m = distance((prev.latitude, prev.longitude), (curr.latitude, curr.longitude)).m
                v = d_m / dt

                # ── LOW-SPEED block ──────────────────────────────────────
                if v < min_speed:  # inside a soft pause
                    if p_start_idx is None:  # first time we dip below v_min
                        p_start_idx, p_start_time = i, prev.time
                        p_drift = 0.0
                    p_drift += d_m
                    continue

                # ── LEAVING a soft pause ────────────────────────────────
                if p_start_idx is not None:
                    gap = _ts(curr.time) - _ts(p_start_time)

                    if gap.total_seconds() >= min_pause_duration:
                        # amount of that gap we must PRESERVE to maintain speed
                        v_avg = moving_dist / moving_time if moving_time else 0.0
                        keep = p_drift / v_avg if v_avg else 1.0
                        keep = min(keep, gap.total_seconds())  # never > gap
                        cut = gap - datetime.timedelta(seconds=keep)
                        # record stats
                        stats["pauses"].append(dict(start=p_start_time, gap=gap, removed=cut, drift=p_drift))
                        stats["removed_time"] += cut
                        stats["pause_drift"] += p_drift
                        stats["cum_shift"] += cut
                        cum_shift += cut
                    else:  # pause too short → keep intact
                        for j in range(p_start_idx, i + 1):
                            last_written = _append(dst_seg, pts[j], shift=cum_shift, last_time=last_written)

                    p_start_idx = p_start_time = None
                    p_drift = 0.0

                # point is normal moving data
                last_written = _append(dst_seg, curr, shift=cum_shift, last_time=last_written)
                moving_dist += d_m
                moving_time += dt

            # ── SOFT pause that reaches end of segment ──────────────────
            if p_start_idx is not None:
                gap = _ts(pts[-1].time) - _ts(p_start_time)
                if gap.total_seconds() >= min_pause_duration:
                    v_avg = moving_dist / moving_time if moving_time else 0.0
                    keep = p_drift / v_avg if v_avg else 1.0
                    keep = min(keep, gap.total_seconds())
                    cut = gap - datetime.timedelta(seconds=keep)

                    stats["pauses"].append(dict(start=p_start_time, gap=gap, removed=cut, drift=p_drift))
                    stats["removed_time"] += cut
                    stats["pause_drift"] += p_drift
                    stats["cum_shift"] += cut
                    cum_shift += cut
                else:  # short pause → keep
                    for j in range(p_start_idx, len(pts)):
                        last_written = _append(dst_seg, pts[j], shift=cum_shift, last_time=last_written)

            # ── HARD pause (gap between segments) ───────────────────────
            nxt = seg_idx + 1
            if nxt < len(src_trk.segments) and src_trk.segments[nxt].points:
                last_pt = src_seg.points[-1]
                first_nx = src_trk.segments[nxt].points[0]
                dt_gap = (_ts(first_nx.time) - _ts(last_pt.time)).total_seconds()
                if dt_gap >= min_pause_duration:
                    d_gap = distance((last_pt.latitude, last_pt.longitude), (first_nx.latitude, first_nx.longitude)).m
                    v_avg = moving_dist / moving_time if moving_time else 0.0
                    keep = d_gap / v_avg if v_avg else 1.0
                    keep = min(keep, dt_gap)
                    cut = datetime.timedelta(seconds=dt_gap - keep)

                    stats["pauses"].append(
                        dict(start=last_pt.time, gap=datetime.timedelta(seconds=dt_gap), removed=cut, drift=d_gap)
                    )
                    stats["removed_time"] += cut
                    stats["pause_drift"] += d_gap
                    stats["cum_shift"] += cut
                    cum_shift += cut

    # ── overall elapsed times ───────────────────────────────────────────
    stats["activity_start"] = _ts(original.tracks[0].segments[0].points[0].time)
    stats["orig_elapsed"] = _ts(original.tracks[-1].segments[-1].points[-1].time) - _ts(
        original.tracks[0].segments[0].points[0].time
    )
    stats["trimmed_elapsed"] = _ts(trimmed.tracks[-1].segments[-1].points[-1].time) - _ts(
        trimmed.tracks[0].segments[0].points[0].time
    )

    return trimmed.to_xml(prettyprint=True), stats


def run_pause_trimmer(
    input_path: str | Path,
    *,
    min_speed: float = 0.1,
    min_pause_duration: int = 240,
) -> None:
    """
    Trim every GPX track in *input_path*.

    Args:
        input_path: Either a single ``.gpx`` file or a ``.zip`` containing many GPX
            files (any sub-folder layout is preserved).
        min_speed : Low-speed threshold in m s⁻¹ (default 0.1).
        min_pause_duration : Minimum pause duration in seconds before it is removed (default 600).
    """
    input_path = Path(input_path)

    # ── helper for one GPX blob ────────────────────────────────────
    def _trim_and_report(xml: str, label: str) -> str:
        gpx = gpxpy.parse(xml)
        xml_out, stats = _trim_track(gpx, min_speed=min_speed, min_pause_duration=min_pause_duration)

        print(f"\n=== {label} ===\n")
        _print_pause_summary(stats, tz_offset=0)
        return xml_out

    # ── single GPX on disk ────────────────────────────────────────
    if input_path.suffix.lower() != ".zip":
        xml_in = input_path.read_text(encoding="utf-8", errors="replace")
        xml_out = _trim_and_report(xml_in, input_path.name)

        out_file = input_path.with_stem(input_path.stem + "_trimmed")
        out_file.write_text(xml_out, encoding="utf-8")
        print(f"\nCreated {out_file.name}")

        return

    # ── ZIP archive ───────────────────────────────────────────────
    out_zip = input_path.with_stem(input_path.stem + "_trimmed")
    trimmed_count = 0

    with zipfile.ZipFile(input_path) as zin, zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zout:

        # walk all entries
        for member in zin.infolist():
            arcname = _decode_name(member)  # repaired text
            p = Path(arcname)

            # skip non-GPX or macOS “resource-fork” files
            if p.suffix.lower() != ".gpx" or p.name.startswith("._"):
                continue

            # read → trim → write back
            xml_in = zin.read(member).decode("utf-8", errors="replace")
            xml_out = _trim_and_report(xml_in, p.name)

            out_name = p.with_stem(p.stem + "_trimmed").as_posix()
            zout.writestr(out_name, xml_out.encode("utf-8"))
            trimmed_count += 1

    if trimmed_count:
        print(f"\nCreated {out_zip.name} with {trimmed_count} trimmed track(s).")
    else:
        print("No .gpx files found in the archive.")


if __name__ == "__main__":
    """Main entry point for command-line usage."""

    import argparse

    parser = argparse.ArgumentParser(prog="gpx_trimmer")
    parser.add_argument(
        "--min_speed",
        default=0.1,
        type=float,
        help="Minimum speed in m/s; points below this are considered part of a pause.",
    )
    parser.add_argument(
        "--min_pause_duration",
        default=240,
        type=int,
        help="Minimum pause duration in seconds; pauses longer than this will be trimmed.",
    )
    parser.add_argument("input_file_path", help="Input file path")
    args = parser.parse_args()

    run_pause_trimmer(args.input_file_path, min_speed=args.min_speed, min_pause_duration=args.min_pause_duration)
