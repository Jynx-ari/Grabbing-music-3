import os
import sys
import subprocess
import time
import yt_dlp
import miniaudio
import threading
from collections import deque
import random

# Set stdout encoding to UTF-8 to support emojis
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': False, # Support playlists
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
}

class MusicPlayer:
    def __init__(self):
        self.queue = deque()
        self.current_song = None
        self.next_song = None # Preloaded song
        self.state = "STOPPED" # PLAYING, PAUSED, STOPPED
        self.ffmpeg_process = None
        self.next_ffmpeg_process = None # Preloaded process
        self.device = None
        self.lock = threading.Lock()
        self.song_finished = False 

    def add_to_queue(self, query):
        print(f"🔎 Searching/Fetching: '{query}'...")
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ytdl:
            try:
                data = ytdl.extract_info(query, download=False)
                if 'entries' in data:
                    # It's a playlist or search result
                    entries = data['entries']
                    count = 0
                    for entry in entries:
                        if entry:
                            self.queue.append({'title': entry.get('title', 'Unknown'), 'url': entry.get('url')})
                            count += 1
                    print(f"✅ Added {count} songs to the queue.")
                else:
                    # It's a single song
                    self.queue.append({'title': data.get('title', 'Unknown'), 'url': data.get('url')})
                    print(f"✅ Added 1 song to the queue.")
            except Exception as e:
                print(f"❌ Error fetching: {e}")

    def preload_next(self):
        """Pre-spawns the FFmpeg process for the next song in the queue."""
        if self.next_ffmpeg_process is not None:
            return

        if not self.queue:
            return

        song = self.queue.popleft()
        self.next_song = song
        
        url = song['url']
        ffmpeg_cmd = [
            'ffmpeg',
            '-reconnect', '1',
            '-reconnect_at_eof', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-i', url,
            '-f', 's16le', '-acodec', 'pcm_s16le', '-ar', '44100', '-ac', '2', '-'
        ]
        
        try:
            self.next_ffmpeg_process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            print(f"❌ Preload Error: {e}")
            self.next_song = None

    def shuffle(self):
        with self.lock:
            # Only shuffle the remaining queue; the preloaded song stays as 'next'
            items = list(self.queue)
            random.shuffle(items)
            self.queue = deque(items)
            print("🔀 Queue shuffled (Next song preserved).")

    def toggle_pause(self):
        with self.lock:
            if self.state == "PLAYING":
                self.state = "PAUSED"
                print("⏸️ Paused")
            elif self.state == "PAUSED":
                self.state = "PLAYING"
                print("▶️ Playing")

    def stop(self):
        with self.lock:
            self.state = "STOPPED"
            if self.ffmpeg_process:
                self.ffmpeg_process.terminate()
            if self.next_ffmpeg_process:
                self.next_ffmpeg_process.terminate()
                self.next_ffmpeg_process = None
                self.next_song = None
            print("⏹️ Stopped")

    def play_next(self, silent=False):
        with self.lock:
            self.song_finished = False 
            if self.ffmpeg_process:
                self.ffmpeg_process.terminate()
                self.ffmpeg_process.wait()
            
            # Use preloaded song if available
            if self.next_ffmpeg_process:
                self.ffmpeg_process = self.next_ffmpeg_process
                self.current_song = self.next_song
                self.next_ffmpeg_process = None
                self.next_song = None
            elif self.queue:
                # Fallback: Pop from queue and spawn immediately
                self.current_song = self.queue.popleft()
                url = self.current_song['url']
                ffmpeg_cmd = [
                    'ffmpeg',
                    '-reconnect', '1',
                    '-reconnect_at_eof', '1',
                    '-reconnect_streamed', '1',
                    '-reconnect_delay_max', '5',
                    '-i', url,
                    '-f', 's16le', '-acodec', 'pcm_s16le', '-ar', '44100', '-ac', '2', '-'
                ]
                self.ffmpeg_process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL
                )
            else:
                if not silent:
                    print("📭 Queue is empty!")
                self.state = "STOPPED"
                self.current_song = None
                return

            self.state = "PLAYING"
            print(f"🎶 Now playing: {self.current_song['title']}")
            
            # Trigger preload for the NEXT next song
            self.preload_next()

def master_generator(player, channels=2, sample_width=2):
    # Initial priming for miniaudio
    required_frames = yield None
    
    while True:
        # 1. Ensure we have a song to play
        if player.current_song is None or player.ffmpeg_process is None:
            player.play_next(silent=True)
            if player.current_song is None:
                # Queue empty? Yield silence and wait
                frames = required_frames if required_frames else 1024
                required_frames = yield b'\x00' * (frames * channels * sample_width)
                continue

        # 2. Lock in the current process for this song session
        current_proc = player.ffmpeg_process
        source_pipe = current_proc.stdout
        
        # 3. Inner loop: Feed data from the current process
        while True:
            with player.lock:
                state = player.state
                # If manual skip happened or user stopped, break to switch songs
                if current_proc != player.ffmpeg_process or state == "STOPPED":
                    break
                if player.song_finished:
                    break

            frames = required_frames if required_frames else 1024
            
            if state == "PAUSED":
                silence = b'\x00' * (frames * channels * sample_width)
                required_frames = yield silence
                continue

            # Read data from the current FFmpeg process
            required_bytes = frames * channels * sample_width
            sample_data = source_pipe.read(required_bytes)
            
            if not sample_data:
                with player.lock:
                    player.song_finished = True
                break
            
            # Yield data and capture the next frame count
            required_frames = yield sample_data

        # 4. Cleanup the process we just finished before moving to the next song
        if current_proc:
            current_proc.terminate()
            current_proc.wait()

def playback_loop(player):
    try:
        with miniaudio.PlaybackDevice(
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=2,
            sample_rate=44100
        ) as device:
            player.device = device
            
            # Create the Master Generator
            gen = master_generator(player)
            
            # Prime the Master Generator once
            next(gen)
            
            # START THE DEVICE ONCE AND ONLY ONCE
            device.start(gen)
            
            # Keep the playback thread alive as long as the program is running
            while True:
                time.sleep(1)
                
    except Exception as e:
        print(f"❌ Playback Error: {e}")

if __name__ == "__main__":
    player = MusicPlayer()
    playback_thread = threading.Thread(target=playback_loop, args=(player,), daemon=True)
    playback_thread.start()

    print("\n🎵 Welcome to Kilo-Music Player 🎵")
    print("Commands: \n - a <query> : Add song/playlist\n - n : Next song\n - p : Pause/Play\n - s : Stop\n - sh : Shuffle\n - q : Show queue\n - exit : Quit\n")

    while True:
        cmd_input = input(">> ").strip()
        if not cmd_input: continue
        if cmd_input.lower() == 'exit': break
        elif cmd_input.lower() == 'n': player.play_next(silent=False)
        elif cmd_input.lower() == 'p': player.toggle_pause()
        elif cmd_input.lower() == 's': player.stop()
        elif cmd_input.lower() == 'sh': player.shuffle()
        elif cmd_input.lower() == 'q':
            with player.lock:
                print("\n--- 🎵 Music Queue ---")
                if player.current_song:
                    print(f"▶️ NOW PLAYING: {player.current_song['title']}")
                
                if not player.queue:
                    print(" (Queue is empty)")
                else:
                    for i, song in enumerate(player.queue):
                        print(f"  {i+1}. {song['title']}")
                print("----------------------\n")

        elif cmd_input.lower().startswith('a '):
            player.add_to_queue(cmd_input[2:].strip())
        else:
            print("❓ Unknown command.")