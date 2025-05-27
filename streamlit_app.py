"""
Streamlit GPX Trimmer app

Run with:
    streamlit run streamlit_app.py

The app wraps the ``run_pause_trimmer`` function from *gpx_trimmer.py* to
trim long pauses from a single GPX track or a batch of tracks inside a ZIP
archive. Users can adjust the low‑speed threshold and minimum pause
duration used to identify long pauses.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
from pathlib import Path

import streamlit as st

from gpx_trimmer import run_pause_trimmer


def main() -> None:
    """Entry‑point for the Streamlit UI."""

    # ── Page config ────────────────────────────────────────────────────
    st.set_page_config(
        page_title="GPX Trimmer",
        page_icon="🏃",
        layout="centered",
    )

    # ── Header / intro ────────────────────────────────────────────────
    st.title("GPX Trimmer 🏃")

    st.markdown("A lightweight tool to trim long pauses from GPX tracks.")

    st.markdown(
        "Some applications, e.g., Komoot, do not differentiate between **moving time** and **elapsed time** when"
        " the activity is imported as a GPX file. In such cases, the elapsed time remains inflated by"
        " the time spent paused during the activity, which does not reflect the actual activity statistics,"
        " especially if a long pause was taken, e.g., during a lunch break."
    )
    st.markdown(
        "This application detects the **long** pauses in your GPX file and removes them, allowing you to obtain a"
        " more accurate representation of your activity.\n"
        "* It only shifts the timestamps of the recorded points to remove long pauses; it does not modify other"
        " GPX data.\n"
        "* To avoid spikes in the velocity profile, the timestamps of points after a long pause are shifted so that"
        " the transition speed matches the moving average speed.\n"
        "* It targets both cases: 1) the GPX tracker paused tracking and there is a large time jump; 2) the GPX"
        " tracker continued recording during a pause.\n"
        "* The data acquisition frequency does not matter.\n"
        "* It provides a short summary of the pauses that are removed."
    )
    st.markdown("")
    st.markdown(
        "Feel free to report any bugs or suggestions via [Github Issues](https://github.com/ozhanozen/gpx-trimmer/issues)."
    )
    st.markdown("---")

    st.subheader("How to Trim")
    st.markdown(
        "* Upload either a single **.gpx** file or a **.zip** archive containing many GPX files.\n"
        "* Adjust the **low‑speed threshold** and **minimum pause duration** to define what is considered a"
        " long pause.\n"
        "* Click **Trim**; the processed file will be offered for download. You can also see the processing summary"
        " in the text area below.\n"
    )
    st.markdown("---")

    # ── File upload ───────────────────────────────────────────────────
    uploaded_file = st.file_uploader(
        "📂 Upload a GPX file or ZIP archive",
        type=["gpx", "zip"],
        accept_multiple_files=False,
    )

    # ── Parameter inputs ──────────────────────────────────────────────
    col1, col2 = st.columns(2)
    with col1:
        min_speed = st.number_input(
            "Low‑speed threshold (m/s)",
            value=0.1,
            step=0.001,
            format="%.3f",
            help="Points moving slower than this are considered part of a pause.",
        )
    with col2:
        min_pause = st.number_input(
            "Minimum pause duration (s)",
            value=240,
            step=1,
            help="Only pauses longer than this will be removed.",
        )

    # ── Process button ────────────────────────────────────────────────
    if uploaded_file is not None and st.button("Trim"):
        with st.spinner("Processing... this may take a moment ☕"):
            # Save the upload to a temporary file so run_pause_trimmer can work with paths.
            with tempfile.TemporaryDirectory() as tmpdir:
                in_path = Path(tmpdir) / uploaded_file.name
                in_path.write_bytes(uploaded_file.getbuffer())

                # Capture stdout from run_pause_trimmer so we can show it in the UI.
                log_stream = io.StringIO()
                with contextlib.redirect_stdout(log_stream):
                    run_pause_trimmer(
                        str(in_path),
                        min_speed=float(min_speed),
                        min_pause_duration=int(min_pause),
                    )
                log_text = log_stream.getvalue()

                # Determine where the trimmed file was written.
                out_path = in_path.with_stem(in_path.stem + "_trimmed")

                if not out_path.exists():
                    st.error("Processing completed but the trimmed file could not be found.")
                    st.code(log_text)
                    return

                # Read the trimmed GPX/ZIP for download.
                trimmed_data = out_path.read_bytes()
                mime = "application/zip" if out_path.suffix.lower() == ".zip" else "application/gpx+xml"

        # ── Display results ───────────────────────────────────────────
        st.success("Done! See the summary below and download your trimmed file(s).")
        st.code(log_text, language="text")
        st.download_button(
            label="📥 Download trimmed file(s)",
            data=trimmed_data,
            file_name=out_path.name,
            mime=mime,
        )


if __name__ == "__main__":
    main()
