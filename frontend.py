"""
Voxa V1 Exploratory Prototype: Streamlit Voice/Text Interactive UI.

Historical context:
Demonstrates initial client-side voice capture, local Whisper transcription,
and gTTS playback connecting to the synchronous V1 Flask inference server.
"""

import os
import time
import uuid
import wave
import requests
import streamlit as st

# Optional native multimedia bindings
try:
    import whisper
except ImportError:
    whisper = None

try:
    import pyaudio
except ImportError:
    pyaudio = None

try:
    from gtts import gTTS
except ImportError:
    gTTS = None

try:
    import pygame
except ImportError:
    pygame = None

# Streamlit Page Configuration
st.set_page_config(page_title="Voxa Support Prototype (V1)", page_icon="🎙", layout="centered")

st.markdown(
    """
    <style>
    .main { background-color: #0d1117; color: #c9d1d9; }
    .stButton button {
        background-color: #238636;
        color: #ffffff;
        border: 1px solid rgba(240, 246, 252, 0.1);
        border-radius: 6px;
        padding: 8px 16px;
        font-weight: 600;
        transition: background-color 0.2s ease;
    }
    .stButton button:hover { background-color: #2ea043; }
    .stTextInput input {
        border-radius: 6px;
        border: 1px solid #30363d;
        background-color: #161b22;
        color: #f0f6fc;
    }
    </style>
    """,
    unsafe_allow_html=True
)

st.title("🎙 Voxa - Voice Support Assistant (V1 Prototype)")
st.caption("Synchronous speech-to-text, Seq2Seq generation, and speech synthesis prototype.")
st.divider()

# Session State Initialization
if "user_input" not in st.session_state:
    st.session_state.user_input = ""
if "voice_error" not in st.session_state:
    st.session_state.voice_error = None


def record_audio(filename: str = "input.wav", duration: int = 10, rate: int = 16000) -> str | None:
    """Captures single-channel PCM audio from the system microphone."""
    if not pyaudio:
        st.session_state.voice_error = "PyAudio is not installed or microphone hardware is unavailable."
        return None

    chunk = 1024
    audio_format = pyaudio.paInt16
    channels = 1
    p = pyaudio.PyAudio()

    try:
        stream = p.open(format=audio_format, channels=channels, rate=rate, input=True, frames_per_buffer=chunk)
        st.info("🎙 Recording active... Speak now.")
        frames = [stream.read(chunk) for _ in range(0, int(rate / chunk * duration))]
        st.success("Recording complete.")

        stream.stop_stream()
        stream.close()
        p.terminate()

        with wave.open(filename, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(p.get_sample_size(audio_format))
            wf.setframerate(rate)
            wf.writeframes(b"".join(frames))

        return filename
    except Exception as exc:
        st.session_state.voice_error = f"Audio capture exception: {exc}"
        return None


def transcribe_with_whisper(audio_path: str) -> str:
    """Transcribes an audio waveform file using OpenAI Whisper."""
    if not whisper:
        return "[ERROR] Whisper library not loaded in runtime."
    try:
        model = whisper.load_model("small")
        result = model.transcribe(audio_path)
        return result.get("text", "").strip()
    except Exception as exc:
        return f"[ERROR] Transcription failed: {exc}"


def speak(text: str) -> None:
    """Synthesizes text to speech using gTTS and plays through pygame mixer."""
    if not gTTS or not pygame:
        return
    filename = f"voxa_{uuid.uuid4().hex[:8]}.mp3"
    try:
        tts = gTTS(text=text, lang="en")
        tts.save(filename)

        pygame.mixer.init()
        pygame.mixer.music.load(filename)
        pygame.mixer.music.play()

        while pygame.mixer.music.get_busy():
            time.sleep(0.05)

        pygame.mixer.music.unload()
        pygame.mixer.quit()
    except Exception as exc:
        st.error(f"Speech playback error: {exc}")
    finally:
        if os.path.exists(filename):
            try:
                os.remove(filename)
            except OSError:
                pass


# Voice Ingestion Control
st.subheader("Voice Input")
if st.button("🎙 Record Voice Input (10s)"):
    audio_path = record_audio(duration=10)
    if audio_path and os.path.exists(audio_path):
        transcription = transcribe_with_whisper(audio_path)
        if not transcription.startswith("[ERROR]"):
            st.session_state.user_input = transcription
            st.success(f"Transcribed: {transcription}")
            st.session_state.voice_error = None
        else:
            st.session_state.voice_error = transcription
        try:
            os.remove(audio_path)
        except OSError:
            pass

if st.session_state.voice_error:
    st.error(st.session_state.voice_error)

# Text Query Control
st.subheader("Text Input")
st.session_state.user_input = st.text_input(
    "Customer Support Query",
    value=st.session_state.user_input,
    placeholder="e.g. How can I initiate a return for my order?"
)

# Inference Request Dispatch
if st.button("Submit Query"):
    query_text = st.session_state.user_input.strip()
    if not query_text:
        st.warning("Please supply a valid query via microphone or text input.")
    else:
        with st.spinner("Dispatching to V1 inference runtime..."):
            backend_url = os.environ.get("BACKEND_URL", "http://localhost:5000/chat")
            try:
                resp = requests.post(backend_url, json={"message": query_text}, timeout=30)
                resp.raise_for_status()
                result_text = resp.json().get("response", "")
                
                st.markdown(f"**Response:** {result_text}")
                speak(result_text)
            except requests.exceptions.ConnectionError:
                st.error(f"Could not connect to inference host at {backend_url}. Verify backend.py is active.")
            except requests.exceptions.Timeout:
                st.error("Request timed out during model inference.")
            except Exception as exc:
                st.error(f"Inference gateway error: {exc}")

st.divider()
st.caption("Voxa Architecture Evolution • V1 Synchronous Prototype • For production workloads, deploy Voxa 2.0 (FastAPI + Redis Streams + Next.js)")

