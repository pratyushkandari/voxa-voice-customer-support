# ------------------------------- IMPORTS -------------------------------

# Import time module to create delays and wait during audio playback
import time

# os module lets us interact with the file system (e.g., save/delete audio files)
import os

# Streamlit is used to create interactive web apps in Python
import streamlit as st

# requests is used to send HTTP requests to backend (Flask server in our case)
import requests

# whisper is OpenAI's library for speech-to-text (transcription)
import whisper

# pyaudio is used to access the system microphone and record voice
import pyaudio

# wave module is used to handle WAV audio files (like saving mic recordings)
import wave

# gTTS (Google Text-to-Speech) converts text into audio
from gtts import gTTS

# pygame is used to play audio (MP3) inside the Streamlit app
import pygame

# uuid helps generate unique filenames so audio files don’t conflict
import uuid

# ------------------------------- STREAMLIT UI SETUP -------------------------------

# Configure the Streamlit page (title, layout)
st.set_page_config(page_title="Voxa Chatbot", layout="centered")

# Inject custom HTML/CSS into Streamlit for styling the UI elements
st.markdown(
    """
    <style>
    html, body, [class*="css"]  {
        margin: 0;
        padding: 0;
        height: 100%;
        overflow: auto;
    }

    .main {
        background-color: #f4f6f7;
        padding: 1.5rem 2rem;
    }

    .stButton button {
        background-color: #4CAF50;
        color: white;
        border: none;
        padding: 8px 18px;
        border-radius: 8px;
        font-size: 0.95rem;
    }

    .stTextInput input {
        padding: 10px;
        border-radius: 8px;
        border: 1px solid #ccc;
    }

    .block-container {
        padding-top: 1rem;
        padding-bottom: 1rem;
    }
    </style>
    """,
    unsafe_allow_html=True  # Allows HTML/CSS to be safely rendered
)

# ------------------------------- HEADER SECTION -------------------------------

# Title of the app
st.title("🤖 Voxa - Your AI Customer Support Assistant")

# Small description text below the title
st.markdown("##### Ask anything via **voice** or **text** — Voxa's got your back!")

# Horizontal divider line
st.divider()

# ------------------------------- SESSION STATE -------------------------------

# Save user input in session state so it persists between re-renders
if "user_input" not in st.session_state:
    st.session_state.user_input = ""

# Store any voice-related errors to show to the user
if "voice_error" not in st.session_state:
    st.session_state.voice_error = None

# ------------------------------- VOICE RECORDING SECTION -------------------------------

# Function to record user's voice through microphone and save it as a WAV file
def record_audio(filename="input.wav", duration=10, rate=16000):
    chunk = 1024  # Size of each audio frame
    format = pyaudio.paInt16  # Audio format (16-bit)
    channels = 1  # Mono recording
    p = pyaudio.PyAudio()  # Initialize pyaudio

    try:
        # Open audio stream to record from the mic
        stream = p.open(format=format,
                        channels=channels,
                        rate=rate,
                        input=True,
                        frames_per_buffer=chunk)

        st.info("🎙 Recording... Please speak clearly into your mic.")
        frames = []  # List to hold recorded audio frames

        # Record for 'duration' seconds
        for _ in range(0, int(rate / chunk * duration)):
            data = stream.read(chunk)  # Read audio chunk from mic
            frames.append(data)  # Append to list

        st.success("✅ Recording complete!")

        # Stop and close audio stream
        stream.stop_stream()
        stream.close()
        p.terminate()

        # Save the recorded frames to a .wav file
        wf = wave.open(filename, 'wb')
        wf.setnchannels(channels)
        wf.setsampwidth(p.get_sample_size(format))
        wf.setframerate(rate)
        wf.writeframes(b''.join(frames))  # Write all frames into file
        wf.close()

        return filename  # Return the path to saved audio

    except Exception as e:
        # Handle microphone-related errors
        st.session_state.voice_error = f"🎤 Microphone error: {e}"
        return None

# Function to convert recorded voice into text using Whisper
def transcribe_with_whisper(audio_path):
    try:
        model = whisper.load_model("small")  # Load Whisper model (small size)
        result = model.transcribe(audio_path)  # Transcribe audio file
        return result["text"]  # Return just the transcribed text
    except Exception as e:
        return f"[ERROR] Whisper failed: {e}"  # Return error if any

# ------------------------------- VOICE UI INTERACTION -------------------------------

# Display voice section for users who want to speak instead of type
st.markdown("🎤 **Prefer speaking? Use the button below:**")

# Record voice when button is clicked
if st.button("🎙 Speak Your Question (10 sec)"):
    audio_path = record_audio(duration=10)  # Record 10 sec audio
    if audio_path and os.path.exists(audio_path):  # Check if recording succeeded
        transcription = transcribe_with_whisper(audio_path)  # Transcribe audio
        if not transcription.startswith("[ERROR]"):  # Check if transcription succeeded
            st.session_state.user_input = transcription  # Store user input
            st.success(f"🗣 You said: **{transcription}**")  # Show what user said
            st.session_state.voice_error = None  # Clear errors
        else:
            st.session_state.voice_error = transcription  # Save transcription error
        os.remove(audio_path)  # Delete temporary audio file
    else:
        st.session_state.voice_error = "❌ Audio recording failed. Please try again with a working mic."

# Show any microphone or transcription errors
if st.session_state.voice_error:
    st.error(st.session_state.voice_error)

# ------------------------------- TEXT INPUT UI -------------------------------

# Display input box for users who prefer typing
st.markdown("📝 **Or type your question below:**")
st.session_state.user_input = st.text_input(
    "Type your question here...",  # Label
    st.session_state.user_input,   # Default value
    placeholder="e.g. What is your return policy?"  # Greyed out hint
)

# ------------------------------- TEXT-TO-SPEECH PLAYBACK -------------------------------

# Function to convert text to speech and play it using gTTS + pygame
def speak(text):
    try:
        filename = f"voxa_{uuid.uuid4()}.mp3"  # Create a unique MP3 filename
        tts = gTTS(text=text, lang='en')  # Convert text to speech using gTTS
        tts.save(filename)  # Save MP3 file

        pygame.mixer.init()  # Initialize audio mixer
        pygame.mixer.music.load(filename)  # Load MP3 file
        pygame.mixer.music.play()  # Start playing

        # Wait while audio is playing
        while pygame.mixer.music.get_busy():
            time.sleep(0.1)

        pygame.mixer.music.unload()  # Unload current audio
        pygame.mixer.quit()  # Quit mixer

        os.remove(filename)  # Delete audio file after playing

    except Exception as e:
        st.error(f"🔊 TTS Playback failed: {e}")  # Show error if any

# ------------------------------- API INTERACTION + TTS OUTPUT -------------------------------

# When user clicks on "Get Answer"
if st.button("💬 Get Answer"):
    if st.session_state.user_input.strip() == "":  # Check for empty input
        st.warning("⚠️ Please enter or speak a valid question.")
    else:
        # Show spinner while processing
        with st.spinner("🤖 Voxa is processing your question..."):
            try:
                # Send user's question to backend Flask API
                response = requests.post(
                    "http://localhost:5000/chat",  # Local Flask server URL
                    json={"message": st.session_state.user_input},  # Send question as JSON
                    timeout=60  # Timeout after 60 seconds
                )

                # Parse response from backend
                result = response.json()["response"]
                st.success("🤖 Voxa says:")  # Header
                st.markdown(f"**💡 {result}**")  # Show the answer in bold
                st.info(f"🧠 Token count: {len(result.split())} words")  # Show word count

                speak(result)  # Read the answer aloud

            except requests.exceptions.ConnectionError:
                st.error("❌ Could not connect to backend server. Is it running?")
            except requests.exceptions.Timeout:
                st.error("⏱ Request timed out. The server may be slow or busy.")
            except Exception as e:
                st.error(f"⚠️ Unexpected error: {e}")

# ------------------------------- FOOTER -------------------------------

# Add footer at the bottom of the page
st.divider()
st.markdown("""
<p style='text-align: center;'>
Made with ❤️ by the <strong>Voxa Team</strong><br>
<span style='font-size: 0.9em;'>Powered by FLAN-T5, Streamlit, Flask, Whisper & gTTS</span>
</p>
""", unsafe_allow_html=True)
