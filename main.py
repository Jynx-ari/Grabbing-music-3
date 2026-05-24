import os
import sys
import subprocess
import time
import yt_dlp
import miniaudio

# Set stdout encoding to UTF-8 to support emojis
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
}

# generator helper to feed raw PCM bytes into miniaudio
def pcm_stream_generator(source_pipe, channels=2, sample_width=2):
    # Prime the generator
    required_frames = yield b"" 
    
    while True:
        # Calculate exactly how many bytes miniaudio needs for the next chunk
        required_bytes = required_frames * channels * sample_width
        sample_data = source_pipe.read(required_bytes)
        
        # If the stream runs out of data, kill the generator loop
        if not sample_data:
            break
            
        # Feed the precise byte chunk to the audio hardware
        required_frames = yield sample_data

def stream_audio(search_query):
    print(f"🔎 Searching for: '{search_query}'...")
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ytdl:
        try:
            data = ytdl.extract_info(search_query, download=False)
            if 'entries' in data and data['entries']:
                info = data['entries'][0]
            else:
                info = data
        except Exception as e:
            print(f"❌ Search Error: {e}")
            return

    network_url = info['url']
    print(f"🎶 Streaming live from source: {info['title']}")

    ffmpeg_cmd = [
        'ffmpeg',
        '-reconnect', '1',
        '-reconnect_at_eof', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '5',
        '-i', network_url,
        '-f', 's16le', '-acodec', 'pcm_s16le', '-ar', '44100', '-ac', '2', '-'
    ]

    # Spawn the background streaming pipeline
    ffmpeg_process = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL
    )
    try:
        with miniaudio.PlaybackDevice(
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=2,
            sample_rate=44100
        ) as device:
            print("🔊 Streaming chunks live! Press 'Ctrl + C' to stop.")
            gen = pcm_stream_generator(ffmpeg_process.stdout)
            next(gen)  # Prime the generator
            device.start(gen)
            
            # Keep script alive while FFmpeg streams chunks
            while ffmpeg_process.poll() is None:
                time.sleep(0.1)

        if ffmpeg_process.returncode != 0:
            print(f"❌ FFmpeg exited with code {ffmpeg_process.returncode}")
        else:
            print("🎶 Stream ended naturally.")

    except KeyboardInterrupt:
        print("\nStopping audio stream...")
    finally:
        # Safely shut down background processes 
        ffmpeg_process.terminate()
        ffmpeg_process.wait()
        print("Done. Storage and RAM completely clean.")
        query = input("Enter song name or URL: ")
        stream_audio(query)

if __name__ == "__main__":
    query = input("Enter song name or URL: ")
    stream_audio(query)
