#!/usr/bin/env python3

import sys
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gst', '1.0')
gi.require_version('GstPbutils', '1.0') # For Discoverer
gi.require_version('GdkPixbuf', '2.0')

import threading
import os # Needed for path manipulation
import importlib.util # Needed for custom import
import html # For decoding HTML entities
import json # For playlist persistence
import base64 # For encoding album art in JSON
import mutagen
import pathlib # <-- ADDED IMPORT
from urllib.parse import urlparse, unquote
from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gst, GstPbutils, GdkPixbuf, Gdk, Pango # Added Gdk and Pango

# --- Import bandcamp scraper manually due to directory name ---
bc_scraper = None
try:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    scraper_path = os.path.join(script_dir, "3rdp", "bandcamp-scraper.py")
    if os.path.exists(scraper_path):
        spec = importlib.util.spec_from_file_location("bc_scraper", scraper_path)
        if spec and spec.loader:
            bc_scraper = importlib.util.module_from_spec(spec)
            # Add to sys.modules to avoid potential re-import issues if needed later
            sys.modules["bc_scraper"] = bc_scraper
            spec.loader.exec_module(bc_scraper)
            print("Successfully imported bandcamp_scraper.")
        else:
             print(f"Warning: Could not create spec/loader for {scraper_path}.", file=sys.stderr)
    else:
        print(f"Warning: Scraper file not found at {scraper_path}", file=sys.stderr)

except Exception as e:
    print(f"Warning: Could not import bandcamp_scraper ({e}). Bandcamp functionality disabled.", file=sys.stderr)
    bc_scraper = None
# --- End manual import ---


# Simple Song data object
class Song(GObject.Object):
    __gtype_name__ = 'Song'

    title = GObject.Property(type=str, default="Unknown Title")
    uri = GObject.Property(type=str) # URI is essential
    artist = GObject.Property(type=str, default="Unknown Artist")
    duration = GObject.Property(type=GObject.TYPE_INT64, default=0) # Store nanoseconds (int64), default to 0
    album_art_data = GObject.Property(type=GLib.Bytes) # Use the class itself for the GType

    def __init__(self, uri, title=None, artist=None, duration=None):
        super().__init__()
        self.uri = uri
        self.title = title if title else "Unknown Title"
        self.artist = artist if artist else "Unknown Artist"
        # Ensure duration is int64, default to 0 if not valid
        self.duration = duration if isinstance(duration, int) and duration >= 0 else 0
        # album_art_data is set later during discovery if found


class NamoWindow(Adw.ApplicationWindow):
    PLAY_ICON = "media-playback-start-symbolic"
    PAUSE_ICON = "media-playback-pause-symbolic"
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.current_song = None
        self._playlist_file_path = os.path.expanduser("~/.config/namo/playlist.json")
        self.duration_ns = 0 # Store duration in nanoseconds
        self._is_seeking = False # Flag to indicate if user is dragging the scale
        self._seek_value_ns = 0 # Store the target seek time during drag
        self._was_playing_before_seek = False # Track player state before seek starts
        self._init_player()
        self._setup_actions() # <-- RESTORED ACTION SETUP CALL

        self.set_title("Namo Media Player")
        self.set_default_size(400, 800)

        # Use ToolbarView for header + content structure
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # --- Header Bar ---
        header = Adw.HeaderBar.new()
        toolbar_view.add_top_bar(header) # Add header to ToolbarView

        # Playback Controls (Placeholder)
        playback_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        playback_box.add_css_class("linked") # Group buttons

        prev_button = Gtk.Button.new_from_icon_name("media-skip-backward-symbolic")
        prev_button.connect("clicked", self._on_prev_clicked)
        playback_box.append(prev_button)

        self.play_pause_button = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        self.play_pause_button.connect("clicked", self.toggle_play_pause)
        playback_box.append(self.play_pause_button)

        next_button = Gtk.Button.new_from_icon_name("media-skip-forward-symbolic")
        next_button.connect("clicked", self._on_next_clicked)
        playback_box.append(next_button)

        header.pack_start(playback_box)

        # --- Main Menu Button (using Gio.Menu) --- <-- RESTORED COMMENT
        main_menu = Gio.Menu()
        # Add items (adjust order/sections as desired)
        main_menu.append("Open Playlist", "win.open_playlist")
        main_menu.append("Save Playlist", "win.save_playlist")
        main_menu.append("Add Folder...", "win.add_folder_new") # Use the unique name
        # Add separator/section if needed
        section = Gio.Menu()
        section.append("About", "win.about")
        main_menu.append_section(None, section)

        menu_button = Gtk.MenuButton.new()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_menu_model(main_menu) # <-- RESTORED set_menu_model

        # Add Song/Album
        add_button = Gtk.Button.new_from_icon_name("list-add-symbolic")
        add_button.connect("clicked", self._on_add_clicked)
        header.pack_end(menu_button) # Menu button packed last
        header.pack_end(add_button)



        # Bandcamp Import Button
        # Get the directory where the script is running
        script_dir = os.path.dirname(os.path.abspath(__file__))
        # Construct the path to the image relative to the script directory
        bc_image_path = os.path.join(script_dir, "bcsymbol.png")
        # Create an image widget from the file
        bc_image = Gtk.Image.new_from_file(bc_image_path)
        # Create a standard button
        import_bc_button = Gtk.Button()
        # Set the image as the button's content
        import_bc_button.set_child(bc_image)
        import_bc_button.set_tooltip_text("Import Bandcamp Album") # Add tooltip
        import_bc_button.connect("clicked", self._on_import_bandcamp_clicked)
        header.pack_end(import_bc_button) # Bandcamp button packed second


        # --- Main Content Box (below header) ---
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        toolbar_view.set_content(main_box) # Set main_box as the content of ToolbarView

        # --- Song Info Area ---
        song_info_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        song_info_box.set_margin_start(12)
        song_info_box.set_margin_end(12)
        song_info_box.set_margin_top(6)
        song_info_box.set_margin_bottom(6)
        main_box.append(song_info_box)

        self.cover_image = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic") # Placeholder icon
        self.cover_image.set_pixel_size(64)
        self.cover_image.add_css_class("album-art-image") # Add CSS class for styling
        song_info_box.append(self.cover_image)

        song_details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        song_details_box.set_valign(Gtk.Align.CENTER)
        song_info_box.append(song_details_box)

        self.song_label = Gtk.Label(label="<Song Title>", xalign=0)
        self.song_label.set_ellipsize(Pango.EllipsizeMode.END) # Enable ellipsizing
        self.song_label.add_css_class("title-5") # Adwaita style class
        song_details_box.append(self.song_label)

        self.time_label = Gtk.Label(label="0:00 / 0:00", xalign=0)
        self.time_label.add_css_class("caption") # Adwaita style class
        song_details_box.append(self.time_label)

        # --- Progress Bar ---
        self.progress_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.progress_scale.set_draw_value(False) # Don't draw the numeric value
        self.progress_scale.set_hexpand(True)
        self.progress_scale.set_sensitive(False) # Start insensitive
        # self.progress_scale.connect("change-value", self._on_progress_seek) # REMOVED: Replaced by GestureDrag
        main_box.append(self.progress_scale)

        # --- Add Drag Gesture for Seeking ---
        drag_controller = Gtk.GestureDrag()
        drag_controller.connect("drag-begin", self._on_seek_drag_begin)
        drag_controller.connect("drag-update", self._on_seek_drag_update)
        drag_controller.connect("drag-end", self._on_seek_drag_end)
        self.progress_scale.add_controller(drag_controller)


        # --- Playlist Area ---
        playlist_header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        playlist_header_box.set_margin_start(12) # Copy margins
        playlist_header_box.set_margin_end(12)

        playlist_label = Gtk.Label(label="Playlist", xalign=0, hexpand=True) # Original label, set hexpand
        playlist_label.add_css_class("title-4")
        playlist_header_box.append(playlist_label)

        self.remaining_time_label = Gtk.Label(label="", xalign=1, halign=Gtk.Align.END) # New label
        self.remaining_time_label.add_css_class("caption") # Use caption style like time label
        playlist_header_box.append(self.remaining_time_label)

        main_box.append(playlist_header_box) # Add the box instead of just the label

        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_vexpand(True)
        scrolled_window.set_hexpand(False)
        scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        main_box.append(scrolled_window)

        # --- Playlist View ---
        self.playlist_store = Gio.ListStore(item_type=Song) # Store for Song objects
        self.playlist_store.connect("items-changed", lambda store, pos, rem, add: self._update_remaining_time()) # Add this line

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_playlist_item_setup)
        factory.connect("bind", self._on_playlist_item_bind)

        self.selection_model = Gtk.SingleSelection(model=self.playlist_store)
        self.selection_model.connect("selection-changed", self._on_playlist_selection_changed)
        self.selection_model.connect("selection-changed", lambda sel, pos, n_items: self._update_remaining_time()) # Add this line

        self.playlist_view = Gtk.ListView(model=self.selection_model,
                                          factory=factory)
        self.playlist_view.set_vexpand(True)
        self.playlist_view.set_vexpand(False)
        scrolled_window.set_child(self.playlist_view)

        # Add key controller for delete/backspace
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_playlist_key_pressed)
        self.playlist_view.add_controller(key_controller)

        # Load previous playlist (default location)
        self._load_playlist()
        self._update_remaining_time() # Add initial call here

    # --- Action Setup --- <-- RESTORED METHOD
    def _setup_actions(self):
        action_group = Gio.SimpleActionGroup()

        open_action = Gio.SimpleAction.new("open_playlist", None)
        open_action.connect("activate", self._on_open_playlist_action)
        action_group.add_action(open_action)

        save_action = Gio.SimpleAction.new("save_playlist", None)
        save_action.connect("activate", self._on_save_playlist_action)
        action_group.add_action(save_action)

        add_folder_action = Gio.SimpleAction.new("add_folder_new", None) # Unique name
        add_folder_action.connect("activate", self._on_add_folder_action)
        action_group.add_action(add_folder_action)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about_action)
        action_group.add_action(about_action)

        self.insert_action_group("win", action_group)

    def _init_player(self):
        """Initialize GStreamer player and discoverer."""
        # Discoverer setup
        self.discoverer = GstPbutils.Discoverer.new(5 * Gst.SECOND) # 5 sec timeout
        self.discoverer.connect("discovered", self._on_discoverer_discovered)
        self.discoverer.connect("finished", self._on_discoverer_finished)
        self.discoverer.start() # Explicitly start the discoverer

        # Player setup
        self.player = Gst.ElementFactory.make("playbin", "player")
        if not self.player:
            print("ERROR: Could not create GStreamer playbin element.", file=sys.stderr)
            # Handle error appropriately (e.g., disable playback features)
            return
        # Create and attach the rgvolume element for ReplayGain
        rgvolume = Gst.ElementFactory.make("rgvolume", "rgvolume")
        if not rgvolume:
            print("ERROR: Could not create rgvolume element.", file=sys.stderr)
            return

        #   Inject rgvolume into the audio path
        self.player.set_property("audio-filter", rgvolume)

        # Set up bus message handling
        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_player_message)

    def play_uri(self, uri):
        """Loads and starts playing a URI."""
        if not self.player:
            self.current_song = None
            return

        # Find the Song object corresponding to the URI
        self.current_song = None
        for i in range(self.playlist_store.get_n_items()):
             song = self.playlist_store.get_item(i)
             if song.uri == uri:
                 self.current_song = song
                 break
# Update the display immediately after identifying the song
        self._update_song_display(self.current_song)

        print(f"Playing URI: {uri}")
        self.player.set_property("uri", uri)
        self.player.set_state(Gst.State.PLAYING)
        # Update button icon immediately for responsiveness
        self.play_pause_button.set_icon_name(self.PAUSE_ICON)
        # Reset progress immediately
        self.duration_ns = 0 # Reset internal duration tracking
        self.progress_scale.set_value(0) # Explicitly reset progress bar UI
        # Timer started via state change message will handle subsequent updates

    def toggle_play_pause(self, button=None):
        """Toggles playback state."""
        if not self.player: return

        state = self.player.get_state(0).state
        if state == Gst.State.PLAYING:
            print("Pausing playback")
            self.player.set_state(Gst.State.PAUSED)
            self.play_pause_button.set_icon_name(self.PLAY_ICON)
        elif state == Gst.State.PAUSED or state == Gst.State.READY:
             # If paused or ready (e.g., after loading but not playing), play
            print("Resuming/Starting playback")
            self.player.set_state(Gst.State.PLAYING)
            self.play_pause_button.set_icon_name(self.PAUSE_ICON)
        elif state == Gst.State.NULL:
            # Need a URI first
            print("No media loaded to play.")
            # Maybe open the 'add' dialog here?
            pass
        # TODO: Handle other states if necessary (e.g., BUFFERING)


    def _on_playlist_item_setup(self, factory, list_item):
        """Setup widgets for a song row."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.set_margin_top(3)
        box.set_margin_bottom(3)

        title_label = Gtk.Label(xalign=0, hexpand=True)
        title_label.set_ellipsize(Pango.EllipsizeMode.END) # Enable ellipsizing
        title_label.set_tooltip_text("") # Set empty tooltip initially
        title_label.set_max_width_chars(40)
        duration_label = Gtk.Label(xalign=1, hexpand=False)
        box.append(title_label)
        box.append(duration_label)
        list_item.set_child(box)

        # Add gesture for double-click activation
        gesture = Gtk.GestureClick.new()
        box.add_controller(gesture)
        # Store the gesture as a Python attribute on the list item
        list_item._click_gesture = gesture



    def _on_playlist_item_bind(self, factory, list_item):
        """Bind song data to the widgets."""
        box = list_item.get_child()
        title_label = box.get_first_child()
        duration_label = box.get_last_child()

        song = list_item.get_item() # Get the Song object

        full_title = f"{song.artist} - {song.title}"
        title_label.set_label(full_title)
        title_label.set_tooltip_text(full_title) # Set full text as tooltip
        # Format duration nicely if it's numeric (nanoseconds)
        # Format duration based on stored nanoseconds (int64)
        duration_ns = song.duration
        if duration_ns != Gst.CLOCK_TIME_NONE and duration_ns > 0:
             seconds = duration_ns // Gst.SECOND
             duration_str = f"{seconds // 60}:{seconds % 60:02d}"
        else:
             duration_str = "--:--" # Default for unknown/invalid

        # title_label was already set above this diff block
        duration_label.set_label(duration_str)

        # Find the gesture added during setup and connect its signal
        # This ensures we connect the signal with the *correct* song object for this row
        # Retrieve the gesture stored during setup
        # Retrieve the gesture stored as an attribute
        gesture = getattr(list_item, "_click_gesture", None)
        if gesture and isinstance(gesture, Gtk.GestureClick):
            # Disconnect previous handler using its ID, if stored
            handler_id = getattr(list_item, "_click_handler_id", None)
            if handler_id:
                try:
                    gesture.disconnect(handler_id)
                except TypeError: # Catch if disconnect fails (e.g., already disconnected)
                    print(f"Warning: Failed to disconnect handler {handler_id}, might be already disconnected.")

            # Connect the new handler and store its ID
            new_handler_id = gesture.connect("pressed", self._on_song_row_activated, song)
            list_item._click_handler_id = new_handler_id
        else:
             print("Warning: Could not find click_gesture on list item during bind.")

    def _on_song_row_activated(self, gesture, n_press, x, y, song):
        """Handles activation (double-click) on a playlist row."""
        # Gtk.GestureClick counts presses; 1 for single, 2 for double
        if n_press == 2:
            print(f"Double-clicked/Activated song: {song.title}")
            if song and song.uri:
                 # Stop current playback before starting new one
                 if self.player:
                     print("Stopping current playback due to activation.")
                     self.player.set_state(Gst.State.NULL)
                 # Play the activated song
                 self.play_uri(song.uri)
            else:
                 print("Cannot play activated item (no URI?).")


    def _on_playlist_selection_changed(self, selection_model, position, n_items):
        """Callback when the selected song in the playlist changes."""
        selected_item = selection_model.get_selected_item()
        if selected_item:
            print(f"Selected: {selected_item.artist} - {selected_item.title}")
            # Update the display with the selected song's info and art
            self._update_song_display(selected_item)
        else:
            # If selection cleared, update display to default state
            self._update_song_display(None)

    def _on_playlist_key_pressed(self, controller, keyval, keycode, state):
        """Handles key presses on the playlist view, specifically Delete/Backspace."""
        if keyval == Gdk.KEY_Delete or keyval == Gdk.KEY_BackSpace:
            position = self.selection_model.get_selected()
            if position != Gtk.INVALID_LIST_POSITION:
                print(f"Deleting item at position: {position}")
                self.playlist_store.remove(position)
                # Selection might change automatically, or we might want to select the next item
                # For now, just removing is sufficient. The selection-changed signal will fire.
                return True # Indicate key press was handled
        return False # Indicate key press was not handled (allow further processing)

    def _on_add_clicked(self, button):
        """Handles the Add button click: shows a Gtk.FileDialog."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Add Music Files")
        dialog.set_modal(True)
        # TODO: Add file filters for common audio types
        # list_store = Gio.ListStore.new(Gtk.FileFilter) # Create ListStore for filters
        # filter_audio = Gtk.FileFilter.new()
        # filter_audio.set_name("Audio Files")
        # filter_audio.add_mime_type("audio/*")
        # list_store.append(filter_audio)
        # dialog.set_filters(list_store)

        dialog.open_multiple(parent=self, cancellable=None,
                             callback=self._on_file_dialog_open_multiple_finish)

    def _on_file_dialog_open_multiple_finish(self, dialog, result):
        """Handles the response from the Gtk.FileDialog."""
        try:
            files = dialog.open_multiple_finish(result)
            if files:
                print(f"Processing {files.get_n_items()} selected items...")
                for i in range(files.get_n_items()):
                    gio_file = files.get_item(i) # Rename to gio_file for clarity
                    if not gio_file: continue

                    try:
                        # Query file info to determine type
                        info = gio_file.query_info(
                            Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
                            Gio.FileQueryInfoFlags.NONE,
                            None
                        )
                        file_type = info.get_file_type()

                        if file_type == Gio.FileType.REGULAR:
                            print(f"Adding regular file: {gio_file.get_uri()}")
                            self._discover_and_add_uri(gio_file.get_uri()) # Existing async discovery for single files
                        elif file_type == Gio.FileType.DIRECTORY:
                            print(f"Starting scan for directory: {gio_file.get_path()}")
                            self._start_folder_scan(gio_file) # New method for folders
                        else:
                            print(f"Skipping unsupported file type: {gio_file.get_path()}")

                    except GLib.Error as info_err:
                         print(f"Error querying info for {gio_file.peek_path()}: {info_err.message}", file=sys.stderr)
                    except Exception as proc_err: # Catch other potential errors processing an item
                         print(f"Error processing item {gio_file.peek_path()}: {proc_err}", file=sys.stderr)

        except GLib.Error as e:
            # Handle errors, specifically checking for user cancellation
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                print("File selection cancelled.")
            else:
                # Treat other GLib errors as actual problems
                print(f"Error opening files: {e.message}", file=sys.stderr)
        except Exception as general_e: # Catch potential errors during finish() itself
             print(f"Unexpected error during file dialog finish: {general_e}", file=sys.stderr)

    def _start_folder_scan(self, folder_gio_file):
        """Starts a background thread to scan a folder for audio files."""
        # print(f"DEBUG: _start_folder_scan called with path: {folder_gio_file.get_path()}") # Removed debug print
        folder_path = folder_gio_file.get_path()
        if folder_path and os.path.isdir(folder_path):
            print(f"Starting background scan thread for: {folder_path}")
            thread = threading.Thread(target=self._scan_folder_thread, args=(folder_path,), daemon=True)
            thread.start()
        else:
            print(f"Cannot scan folder: Invalid path or not a directory ({folder_path})", file=sys.stderr)

    def _scan_folder_thread(self, folder_path):
        """Background thread function to recursively scan a folder for audio files."""
        print(f"Thread '{threading.current_thread().name}': Scanning {folder_path}")
        audio_extensions = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".aac"}
        files_found = 0
        files_added = 0

        try:
            for root, _, filenames in os.walk(folder_path):
                for filename in filenames:
                    if filename.lower().endswith(tuple(audio_extensions)):
                        files_found += 1
                        full_path = os.path.join(root, filename)
                        try:
                            file_uri = pathlib.Path(full_path).as_uri()
                            # Call the *synchronous* discovery helper within the thread
                            song_object = self._discover_uri_sync(file_uri, full_path)

                            if song_object:
                                files_added += 1
                                # Schedule adding the song object to the store on the main thread
                                GLib.idle_add(self.playlist_store.append, song_object)
                                # Optional: Add a small sleep to avoid flooding GLib.idle_add
                                # import time
                                # time.sleep(0.01)
                        except Exception as file_proc_err:
                            print(f"Thread '{threading.current_thread().name}': Error processing file {full_path}: {file_proc_err}", file=sys.stderr)

        except Exception as walk_err:
            print(f"Thread '{threading.current_thread().name}': Error walking directory {folder_path}: {walk_err}", file=sys.stderr)

        print(f"Thread '{threading.current_thread().name}': Finished scanning {folder_path}. Found: {files_found}, Added: {files_added}")


    def _discover_uri_sync(self, uri, filepath):
        """
        Synchronous helper to discover metadata and art for a single file path.
        Runs within the background folder scanning thread.
        Uses Mutagen primarily. Returns a Song object or None.
        """
        # print(f"Sync Discover: {filepath}") # Debug: Uncomment if needed
        mutagen_title = None
        mutagen_artist = None
        album_art_bytes = None
        album_art_glib_bytes = None
        duration_ns = 0 # Default duration

        try:
            if not os.path.exists(filepath):
                 print(f"Sync Discover Error: File path does not exist: {filepath}", file=sys.stderr)
                 return None

            # 1. Attempt Mutagen for Tags + Art
            try:
                # Read easy tags first
                audio_easy = mutagen.File(filepath, easy=True)
                if audio_easy:
                    mutagen_title = audio_easy.get('title', [None])[0]
                    mutagen_artist = audio_easy.get('artist', [None])[0]
                    # Try getting duration from easy tags if available (less common)
                    duration_str = audio_easy.get('length', [None])[0]
                    if duration_str:
                        try: duration_ns = int(float(duration_str) * Gst.SECOND)
                        except (ValueError, TypeError): pass # Ignore if invalid
            except Exception as tag_e:
                print(f"Sync Discover: Mutagen error reading easy tags from {filepath}: {tag_e}", file=sys.stderr)

            # Read raw file for art separately (more reliable check)
            try:
                audio_raw = mutagen.File(filepath)
                if audio_raw:
                    # Get duration from raw info if not found via easy tags
                    if duration_ns <= 0 and audio_raw.info and hasattr(audio_raw.info, 'length'):
                        try: duration_ns = int(audio_raw.info.length * Gst.SECOND)
                        except (ValueError, TypeError): pass

                    # Art Extraction (similar logic to async version)
                    if audio_raw.tags:
                        if isinstance(audio_raw.tags, mutagen.id3.ID3) and 'APIC:' in audio_raw.tags:
                            album_art_bytes = audio_raw.tags['APIC:'].data
                        elif isinstance(audio_raw, mutagen.mp4.MP4) and 'covr' in audio_raw.tags and audio_raw.tags['covr']:
                            album_art_bytes = bytes(audio_raw.tags['covr'][0])
                        elif hasattr(audio_raw, 'pictures') and audio_raw.pictures:
                            album_art_bytes = audio_raw.pictures[0].data

                    # Wrap art bytes if found
                    if album_art_bytes:
                        try:
                            album_art_glib_bytes = GLib.Bytes.new(album_art_bytes)
                        except Exception as wrap_e:
                            print(f"Sync Discover: Error wrapping album art bytes for {filepath}: {wrap_e}", file=sys.stderr)
                            album_art_glib_bytes = None
            except Exception as art_e:
                 print(f"Sync Discover: Mutagen error reading raw file/art tags from {filepath}: {art_e}", file=sys.stderr)

        except Exception as e:
            print(f"Sync Discover: General Mutagen error for {filepath}: {e}", file=sys.stderr)
            # Allow continuing to create Song with defaults if possible

        # 2. Determine final metadata (Primarily from Mutagen)
        # Use filename if title is still None after Mutagen
        final_title = mutagen_title if mutagen_title else os.path.splitext(os.path.basename(filepath))[0]
        final_artist = mutagen_artist # Keep None if Mutagen didn't find it
        # Ensure duration is valid int >= 0
        duration_to_store = duration_ns if isinstance(duration_ns, int) and duration_ns >= 0 else 0

        # 3. Create the Song object
        try:
            song = Song(uri=uri, title=final_title, artist=final_artist, duration=duration_to_store)

            # 4. Assign album art if found and wrapped successfully
            if album_art_glib_bytes:
                 song.album_art_data = album_art_glib_bytes

            # print(f"Sync Discover OK: '{song.title}', Art: {song.album_art_data is not None}") # Debug
            return song
        except Exception as song_create_e:
             print(f"Sync Discover: Error creating Song object for {filepath}: {song_create_e}", file=sys.stderr)
             return None # Critical error creating the object

    # --- Async Discovery (Keep for single files/URIs) ---
    def _discover_and_add_uri(self, uri):
        # ... (existing async discovery code remains unchanged) ...
        print(f"Starting ASYNC discovery for: {uri}") # Clarify it's async
        self.discoverer.discover_uri_async(uri)

    def _on_discoverer_discovered(self, discoverer, info, error):
        """Callback when GstDiscoverer finishes discovering a URI."""
        # ... (existing async discovery callback remains unchanged) ...
        print(f"--- ASYNC _on_discoverer_discovered called for URI: {info.get_uri()} ---") # Clarify
        uri = info.get_uri()

        # Check for errors first
        if error:
            print(f"Error discovering URI: {uri} - {error.message}")
            if error.matches(Gst.DiscovererError.quark(), Gst.DiscovererError.URI_INVALID):
                print("Invalid URI.")
            elif error.matches(Gst.DiscovererError.quark(), Gst.DiscovererError.MISSING_PLUGIN):
                caps_struct = error.get_details() # Gst.Structure
                if caps_struct:
                    print(f"Missing decoder for: {caps_struct.to_string()}")
                else:
                    print("Missing decoder details unavailable.")
            elif error.matches(Gst.DiscovererError.quark(), Gst.DiscovererError.MISC):
                print(f"Misc error: {error.message}")
            return # Stop processing if there's an error

        # If no error, check GstPbutilsDiscovererResult
        result = info.get_result()
        if result == GstPbutils.DiscovererResult.OK:
            gst_tags = info.get_tags()
            duration_ns = info.get_duration()

            # 1. Initialize metadata variables
            mutagen_title = None
            mutagen_artist = None
            album_art_bytes = None
            album_art_glib_bytes = None # Initialize here

            # 2. Attempt Mutagen for local files (Tags + Art)
            if uri.startswith('file://'):
                try:
                    parsed_uri = urlparse(uri)
                    # Ensure path is correctly reconstructed, handling potential leading '/'
                    filepath_parts = [parsed_uri.netloc, unquote(parsed_uri.path)]
                    filepath = os.path.abspath(os.path.join(*filter(None, filepath_parts)))

                    if not os.path.exists(filepath):
                         print(f"Mutagen error: File path does not exist: {filepath}", file=sys.stderr)
                    else:
                        print(f"Attempting to read tags/art with Mutagen: {filepath}")
                        # Read easy tags first
                        try:
                            audio_easy = mutagen.File(filepath, easy=True)
                            if audio_easy:
                                mutagen_title = audio_easy.get('title', [None])[0]
                                mutagen_artist = audio_easy.get('artist', [None])[0]
                        except Exception as tag_e:
                            print(f"Mutagen error reading easy tags from {filepath}: {tag_e}", file=sys.stderr)

                        # Read raw file for art separately
                        try:
                            print(f"Attempting Mutagen raw file load: {filepath}")
                            audio_raw = mutagen.File(filepath)
                            if audio_raw and audio_raw.tags:
                                print(f"Mutagen raw tags found: Type={type(audio_raw.tags)}")
                                if isinstance(audio_raw.tags, mutagen.id3.ID3): # More specific ID3 check
                                    if 'APIC:' in audio_raw.tags:
                                        print("Found APIC tag in ID3.")
                                        album_art_bytes = audio_raw.tags['APIC:'].data
                                    else:
                                        print("No APIC tag found in ID3.")
                                elif isinstance(audio_raw, mutagen.mp4.MP4): # MP4 check
                                    if 'covr' in audio_raw.tags:
                                        artworks = audio_raw.tags['covr']
                                        if artworks:
                                            print("Found covr tag in MP4.")
                                            album_art_bytes = bytes(artworks[0])
                                        else:
                                            print("covr tag found but empty in MP4.")
                                    else:
                                        print("No covr tag found in MP4.")
                                elif hasattr(audio_raw, 'pictures') and audio_raw.pictures: # FLAC/Vorbis etc.
                                    print(f"Found {len(audio_raw.pictures)} pictures in tags.")
                                    album_art_bytes = audio_raw.pictures[0].data
                                else:
                                    print("No known picture tag (APIC, covr, pictures) found.")

                                # Try creating GLib.Bytes immediately if art bytes were found
                                if album_art_bytes:
                                    try:
                                        print(f"Attempting to create GLib.Bytes from art ({len(album_art_bytes)} bytes).")
                                        album_art_glib_bytes = GLib.Bytes.new(album_art_bytes)
                                    except Exception as wrap_e:
                                        print(f"Error wrapping album art bytes: {wrap_e}", file=sys.stderr)
                                        album_art_glib_bytes = None # Reset if wrapping failed
                            else:
                                print(f"Mutagen could not find tags in raw file: {filepath}")

                        except Exception as art_e:
                             print(f"Mutagen error reading raw file/art tags from {filepath}: {art_e}", file=sys.stderr)

                except Exception as e:
                    print(f"General Mutagen error for {uri}: {e}", file=sys.stderr)

            # 3. Get GStreamer tags as fallback
            gst_title = gst_tags.get_string(Gst.TAG_TITLE)[1] if gst_tags and gst_tags.get_string(Gst.TAG_TITLE)[0] else None
            gst_artist = gst_tags.get_string(Gst.TAG_ARTIST)[1] if gst_tags and gst_tags.get_string(Gst.TAG_ARTIST)[0] else None

            # 4. Determine final metadata
            final_title = mutagen_title if mutagen_title is not None else gst_title
            final_artist = mutagen_artist if mutagen_artist is not None else gst_artist
            duration_to_store = duration_ns if isinstance(duration_ns, int) and duration_ns >= 0 else 0

            # 5. Create the Song object
            song_to_add = Song(uri=uri, title=final_title, artist=final_artist, duration=duration_to_store)

            # 6. Assign album art if found and wrapped successfully
            if album_art_glib_bytes: # Check the GLib.Bytes object created earlier
                 try:
                     song_to_add.album_art_data = album_art_glib_bytes
                     print(f"Successfully assigned album art data to Song object for {final_title}")
                 except Exception as assign_e:
                      print(f"Error assigning album art GLib.Bytes: {assign_e}", file=sys.stderr)

            # 7. Final status print
            print(f"Discovered OK: URI='{song_to_add.uri}', Title='{song_to_add.title}', Artist='{song_to_add.artist}', Duration={song_to_add.duration / Gst.SECOND:.2f}s, Art Assigned={song_to_add.album_art_data is not None}")

            # 8. Schedule add
            GLib.idle_add(self.playlist_store.append, song_to_add)
            print(f"Scheduled add for: {final_title or 'Unknown Title'}")

        elif result == GstPbutils.DiscovererResult.TIMEOUT:
             print(f"Discovery Timeout: {uri}", file=sys.stderr)
        elif result == GstPbutils.DiscovererResult.BUSY:
             print(f"Discovery Busy: {uri} - Retrying later?", file=sys.stderr)
        elif result == GstPbutils.DiscovererResult.MISSING_PLUGINS:
             print(f"Discovery Missing Plugins: {uri}", file=sys.stderr)
             # Details might be in the 'error' object if it was set, or might need separate handling if info has details
        else:
             print(f"Discovery Result: {uri} - {result}", file=sys.stderr)


    def _on_discoverer_finished(self, discoverer):
        print("--- _on_discoverer_finished called ---") # Added print

    def _on_player_message(self, bus, message):
        """Handles messages from the GStreamer bus."""
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(f"ERROR: {err.message} ({dbg})", file=sys.stderr)
            # Stop playback on error
            if self.player:
                self.player.set_state(Gst.State.NULL)
                self.play_pause_button.set_icon_name(self.PLAY_ICON)
                self.progress_scale.set_value(0)
                self.progress_scale.set_sensitive(False)
                self.time_label.set_label("0:00 / 0:00")
                self.song_label.set_label("<No Song Playing>")
                self.current_song = None
        elif t == Gst.MessageType.EOS:
            print("End-of-stream reached.")
            if self.player:
                self.player.set_state(Gst.State.NULL) # Or READY? NULL resets pipeline
                self.play_pause_button.set_icon_name(self.PLAY_ICON)
                self.progress_scale.set_value(0)
                self.progress_scale.set_sensitive(False)
                self.time_label.set_label("0:00 / 0:00")
                self.song_label.set_label("<No Song Playing>")
                self.current_song = None
                # Auto-play next song
                print("EOS: Selecting next song.")
                self._on_next_clicked() # Advances selection

                # Now get the newly selected song and play it
                new_pos = self.selection_model.get_selected()
                if new_pos != Gtk.INVALID_LIST_POSITION:
                    next_song = self.playlist_store.get_item(new_pos)
                    if next_song and next_song.uri:
                        print(f"EOS: Playing next song: {next_song.title}")
                        self.play_uri(next_song.uri)
                    else:
                        print("EOS: Next song has no URI or could not be retrieved.")
                else:
                    print("EOS: No next song selected (end of playlist or error).")
        elif t == Gst.MessageType.STATE_CHANGED:
            old_state, new_state, pending_state = message.parse_state_changed()
            # Only care about messages from the playbin itself
            if message.src == self.player:
                print(f"State changed from {old_state.value_nick} to {new_state.value_nick}")
                if new_state == Gst.State.PLAYING:
                    self.play_pause_button.set_icon_name(self.PAUSE_ICON)
                    # self.progress_scale.set_sensitive(True) # Enable seeking - Moved to where duration is known
                    # Start timer if not already running (or ensure it is)
                    if not hasattr(self, '_progress_timer_id') or self._progress_timer_id is None:
                         self._progress_timer_id = GLib.timeout_add_seconds(1, self._update_progress)
                    # Original line moved inside the if block above, so remove the duplicate below
                    # self._progress_timer_id = GLib.timeout_add_seconds(1, self._update_progress) # REMOVED DUPLICATE
                         self._progress_timer_id = GLib.timeout_add_seconds(1, self._update_progress)
                elif new_state == Gst.State.PAUSED:
                    self.play_pause_button.set_icon_name(self.PLAY_ICON)
                    # self.progress_scale.set_sensitive(False) # Removed for debugging
                elif new_state == Gst.State.READY or new_state == Gst.State.NULL:
                    self.play_pause_button.set_icon_name(self.PLAY_ICON)
                    self.progress_scale.set_value(0)
                    # self.progress_scale.set_sensitive(False) # Removed for debugging
                    # Stop timer
                    if hasattr(self, '_progress_timer_id') and self._progress_timer_id is not None:
                        GLib.source_remove(self._progress_timer_id)
                        self._progress_timer_id = None
        elif t == Gst.MessageType.DURATION_CHANGED:
             # This might be called when duration is initially discovered
             self.duration_ns = self.player.query_duration(Gst.Format.TIME)[1]
             print(f"Duration changed: {self.duration_ns / Gst.SECOND:.2f}s")
             if self.duration_ns > 0:
                 self.progress_scale.set_range(0, self.duration_ns / Gst.SECOND)
                 self.progress_scale.set_sensitive(True) # Enable seeking/drag now that duration is known
             else:
                 self.progress_scale.set_range(0, 0) # Set to 0 if duration is invalid/unknown
                 self.progress_scale.set_sensitive(False) # Disable if duration unknown
             GLib.idle_add(self._update_progress) # Update UI immediately


        # Indicate that the message has been handled
        return True
    def _update_song_display(self, song):
        """Updates the song title, artist, time label (0:00 / Duration), and cover art."""
        if song:
            # Update Title/Artist Label
            self.song_label.set_label(f"{song.artist} - {song.title}")
            self.song_label.set_tooltip_text(f"{song.artist} - {song.title}") # Set full text as tooltip

            # Update Time Label (Set to 0:00 / Duration)
            duration_ns = song.duration
            if duration_ns is not None and duration_ns != Gst.CLOCK_TIME_NONE and duration_ns > 0:
                dur_sec = duration_ns // Gst.SECOND
                duration_str = f"{dur_sec // 60}:{dur_sec % 60:02d}"
            else:
                duration_str = "--:--"
            self.time_label.set_label(f"0:00 / {duration_str}")

            # Update Cover Art
            glib_bytes_data = song.album_art_data
            # print(f"_update_song_display: Checking art for {song.title}: Data found = {glib_bytes_data is not None}") # Debug
            if glib_bytes_data:
                raw_bytes_data = glib_bytes_data.get_data() # Extract raw bytes
                try:
                    loader = GdkPixbuf.PixbufLoader()
                    loader.write(raw_bytes_data)
                    loader.close()
                    pixbuf = loader.get_pixbuf()
                    # Scale pixbuf to fit the image view size
                    scaled_pixbuf = pixbuf.scale_simple(64, 64, GdkPixbuf.InterpType.BILINEAR)
                    self.cover_image.set_from_pixbuf(scaled_pixbuf)
                    # print(f"_update_song_display: Pixbuf loaded for {song.title}.") # Debug
                except Exception as e:
                    print(f"Error loading album art in _update_song_display for '{song.title}': {e}", file=sys.stderr)
                    self.cover_image.set_from_icon_name("audio-x-generic-symbolic") # Fallback
            else:
                # print(f"_update_song_display: No art found for {song.title}, setting default icon.") # Debug
                self.cover_image.set_from_icon_name("audio-x-generic-symbolic") # Default icon
        else:
            # Set default states if no song
            # print("_update_song_display: No song provided, setting default display.") # Debug
            self.song_label.set_label("<No Song Playing>")
            self.song_label.set_tooltip_text("") # Clear tooltip when no song
            self.time_label.set_label("0:00 / 0:00")
            self.cover_image.set_from_icon_name("audio-x-generic-symbolic")


    # --- Remaining Time Calculation ---
    def _update_remaining_time(self):
        """Calculates and updates the remaining playlist time label."""
        total_remaining_ns = 0
        selected_index = self.selection_model.get_selected()

        # Start summing from the item *after* the selected one
        # If nothing is selected, sum everything
        start_index = 0
        if selected_index != Gtk.INVALID_LIST_POSITION:
            start_index = selected_index + 1

        n_items = self.playlist_store.get_n_items()
        for i in range(start_index, n_items):
            song = self.playlist_store.get_item(i)
            # Ensure song exists, duration is an int, and is positive
            if song and isinstance(song.duration, int) and song.duration > 0:
                total_remaining_ns += song.duration

        formatted_string = ""
        if total_remaining_ns > 0:
            total_seconds = total_remaining_ns // Gst.SECOND
            total_minutes = total_seconds // 60
            hours = total_minutes // 60
            minutes = total_minutes % 60

            if hours > 0:
                formatted_string = f"{hours}h {minutes}m remaining"
            elif minutes > 0:
                formatted_string = f"{minutes}m remaining"
            elif total_seconds > 0: # Handle case where total time is < 1 minute
                 formatted_string = "<1m remaining"
            # If 0 seconds, formatted_string remains ""

        # Update the label in the main thread
        GLib.idle_add(self.remaining_time_label.set_text, formatted_string)

    # --- Progress Update ---

    def _update_progress(self):
        # Don't update progress via timer if user is actively seeking
        if self._is_seeking:
            # print("DEBUG: _update_progress returning True (seeking).") # Optional debug
            return True # Keep timer running, but don't update scale value
        """Timer callback to update playback progress."""
        if not self.player or not self.current_song:
            # Ensure timer stops if player becomes invalid unexpectedly
            if hasattr(self, '_progress_timer_id') and self._progress_timer_id is not None:
                GLib.source_remove(self._progress_timer_id)
                self._progress_timer_id = None
            return False # Stop timer if no player or song

        state = self.player.get_state(0).state
        if state != Gst.State.PLAYING and state != Gst.State.PAUSED:
            # Stop timer if not playing or paused
            self._progress_timer_id = None
            return False

        # Query duration if we don't have it or it's invalid
        # Query duration if we don't have it or it's invalid (but don't change scale props here)
        if self.duration_ns <= 0:
             ok, new_duration_ns = self.player.query_duration(Gst.Format.TIME)
             if ok:
                 self.duration_ns = new_duration_ns # Update internal value if successful
             else:
                 print("Could not query duration in timer.")
                 self.duration_ns = 0 # Reset if query failed
        # Query position
        ok_pos, position_ns = self.player.query_position(Gst.Format.TIME)
        pos_sec = 0 # Default if query fails
        if ok_pos:
            pos_sec = position_ns // Gst.SECOND # Calculate pos_sec if query succeeded
            # Update time label (always needs position, uses duration if available)
            # Format time based on duration
            label_text = "" # Initialize
            if self.duration_ns > 0:
                 dur_sec = self.duration_ns // Gst.SECOND
                 label_text = f"{pos_sec // 60}:{pos_sec % 60:02d} / {dur_sec // 60}:{dur_sec % 60:02d}"
            else:
                 label_text = f"{pos_sec // 60}:{pos_sec % 60:02d} / --:--"

            self.time_label.set_label(label_text) # Use the calculated text

            # Update scale position (always if position query succeeded)
            adj = self.progress_scale.get_adjustment()
            adj.set_value(pos_sec)


        # Keep timer running only if playing
        # Keep timer running only if playing
        if state == Gst.State.PLAYING:
            return True # Continue timer
        else:
            self._progress_timer_id = None # Ensure timer ID is cleared if paused
            return False # Stop timer


    # --- Seek Gesture Handlers ---

    def _on_seek_drag_begin(self, gesture, start_x, start_y):
        """Called when the user starts dragging the progress scale."""
        print("Seek drag begin")
        self._is_seeking = True
        # Store the player state *before* the drag begins
        self._was_playing_before_seek = (self.player.get_state(0).state == Gst.State.PLAYING)
        print(f"DEBUG: Drag begin. Was playing: {self._was_playing_before_seek}") # Add debug
        # Stop the progress update timer while seeking
        if hasattr(self, '_progress_timer_id') and self._progress_timer_id is not None:
            print("Stopping progress timer for seek")
            GLib.source_remove(self._progress_timer_id)
            self._progress_timer_id = None

    def _on_seek_drag_update(self, gesture, offset_x, offset_y):
        """Called continuously while the user drags the progress scale."""
        if not self._is_seeking: return # Should not happen, but safety check

        adj = self.progress_scale.get_adjustment()
        lower = adj.get_lower()
        upper = adj.get_upper()
        alloc = self.progress_scale.get_allocation()

        if alloc.width == 0: return # Avoid division by zero

        start_x, _ = gesture.get_start_point()
        # Calculate the target value based on the drag position relative to the scale's width
        target_value = lower + (upper - lower) * (start_x + offset_x) / alloc.width
        # Clamp value within the adjustment's bounds
        target_value = max(lower, min(target_value, upper))

        # print(f"Drag update: offset_x={offset_x:.2f}, target_value={target_value:.2f}") # Debug

        # Update the scale's visual position *without* triggering signals
        # that might cause loops or unwanted seeks.

        # Store the calculated seek time (in nanoseconds) for use in drag-end
        self._seek_value_ns = int(target_value * Gst.SECOND)

        # Update the time label immediately to reflect the dragged position
        pos_sec = int(target_value)
        if self.duration_ns > 0:
             dur_sec = self.duration_ns // Gst.SECOND
             self.time_label.set_label(f"{pos_sec // 60}:{pos_sec % 60:02d} / {dur_sec // 60}:{dur_sec % 60:02d}")
        else:
             # Fallback if duration isn't known yet (should be, but safety)
             self.time_label.set_label(f"{pos_sec // 60}:{pos_sec % 60:02d} / --:--")


    def _on_seek_drag_end(self, gesture, offset_x, offset_y):
        """Called when the user releases the progress scale after dragging."""
        print(f"Seek drag end: offset_x={offset_x:.2f}")
        if not self._is_seeking: return # Should not happen

        self._is_seeking = False

        # Perform the actual seek using the value stored during drag-update
        if self.player and self.duration_ns > 0:
            print(f"Performing seek to: {self._seek_value_ns / Gst.SECOND:.2f}s")
            seek_flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT
            if not self.player.seek_simple(Gst.Format.TIME, seek_flags, self._seek_value_ns):
                print("Seek failed.", file=sys.stderr)
            else:
                # Set scale value after seek completes
                final_seek_pos_sec = self._seek_value_ns / Gst.SECOND
                self.progress_scale.set_value(final_seek_pos_sec)

            # Restart the progress timer ONLY if the player was playing before the drag started
            # We check the player's current state. If it's PLAYING, it means it was playing
            # before the drag (since we didn't pause it). If it's PAUSED, it was paused before.
            # If it's READY/NULL, it wasn't playing.
            state = self.player.get_state(0).state
            if state == Gst.State.PLAYING:
                 if not hasattr(self, '_progress_timer_id') or self._progress_timer_id is None:
                     # Use timeout_add with milliseconds for a short delay
                     print("Scheduling delayed timer restart after seek.")
                     GLib.timeout_add(100, self._restart_progress_timer) # Call a helper
            else:
                 print("Not scheduling timer restart (player was not playing before seek)")
        else:
            print("Not seeking: Player invalid or duration unknown.")
    # Helper method to restart the timer after a delay
    def _restart_progress_timer(self):
        if not hasattr(self, '_progress_timer_id') or self._progress_timer_id is None:
             # Check if player was playing *before* the drag started
             if self._was_playing_before_seek:
                 print("Restarting progress timer after seek delay (was playing before).")
                 self._progress_timer_id = GLib.timeout_add_seconds(1, self._update_progress)
             else:
                 print("Not restarting progress timer (was not playing before seek).")
                 self._progress_timer_id = GLib.timeout_add_seconds(1, self._update_progress)
        return GLib.SOURCE_REMOVE # Ensure the timeout only runs once


    # --- Bandcamp Import Methods ---
    def _on_import_bandcamp_clicked(self, button):
        """Shows a dialog to get the Bandcamp album URL."""
        if not bc_scraper:
            print("Bandcamp scraper not available.")
            # TODO: Show an Adw.Toast or error dialog
            return

        dialog = Adw.MessageDialog.new(self,
                                       "Import Bandcamp Album",
                                       "Please enter the full URL of the Bandcamp album:")
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("ok", "_Import")
        dialog.set_default_response("ok")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        # Add an entry field for the URL
        url_entry = Gtk.Entry()
        url_entry.set_placeholder_text("https://artist.bandcamp.com/album/album-name")
        url_entry.set_activates_default(True) # Pressing Enter in entry activates default button ("ok")
        dialog.set_extra_child(url_entry) # Add entry below the main text

        dialog.connect("response", self._on_bandcamp_dialog_response, url_entry)
        dialog.present()


    def _on_bandcamp_dialog_response(self, dialog, response_id, url_entry):
        """Handles the response from the Bandcamp URL dialog."""
        if response_id == "ok":
            url = url_entry.get_text().strip()
            if url:
                # Basic validation (starts with http)
                if url.startswith("http://") or url.startswith("https://"):
                     print(f"Starting Bandcamp import for: {url}")
                     self._start_bandcamp_import(url)
                else:
                     print("Invalid URL entered.")
                     # TODO: Show feedback (e.g., Adw.Toast)
            else:
                 print("No URL entered.")
        else:
            print("Bandcamp import cancelled.")
        # Dialog is automatically destroyed by Adw.MessageDialog

    def _start_bandcamp_import(self, url):
        # Run scraping in a background thread
        print(f"Scheduling import thread for {url}")
        thread = threading.Thread(target=self._run_bandcamp_import_thread, args=(url,), daemon=True)
        thread.start()

    def _run_bandcamp_import_thread(self, url):
        if not bc_scraper: return # Should not happen if button is clicked, but safety check

        print(f"Background thread started for: {url}")
        try:
            # Assuming URL is an album URL for now
            track_infos = bc_scraper.get_album_track_info(url)
            if not track_infos:
                print("No tracks found or error during scraping.")
                # TODO: Show feedback to user via GLib.idle_add
                return

            print(f"Scraped {len(track_infos)} tracks. Adding to playlist...")
            for info in track_infos:
                # Convert duration seconds to display string
                duration_str = "--:--"
                if info.get("duration"):
                    try:
                        seconds = int(float(info["duration"])) # Ensure float then int
                        duration_str = f"{seconds // 60}:{seconds % 60:02d}"
                    except (ValueError, TypeError):
                        pass # Keep default if conversion fails

                # Create Song object - Use stream_url as the URI
                # Ensure stream_url exists before creating song
                stream_url = info.get("stream_url")
                if not stream_url:
                    print(f"Skipping track '{info.get('title')}' - no stream URL found.")
                    continue

                # Decode HTML entities
                title = html.unescape(info.get("title", "Unknown Title"))
                artist = html.unescape(info.get("artist", "Unknown Artist"))

                # Convert BC float seconds to int nanoseconds >= 0
                duration_ns_bc = 0
                if info.get("duration"):
                    try:
                        temp_ns = int(float(info["duration"]) * Gst.SECOND)
                        if temp_ns >= 0:
                             duration_ns_bc = temp_ns
                    except (ValueError, TypeError):
                        pass # Keep 0

                song = Song(uri=stream_url,
                            title=title,
                            artist=artist,
                            duration=duration_ns_bc) # Store nanoseconds

                # Schedule adding to store on the main thread
                GLib.idle_add(self.playlist_store.append, song)
                # Optional: print confirmation after scheduling
                # print(f"Scheduled add for {song.title}")

            print("Finished adding Bandcamp tracks to playlist.")
            # TODO: Show feedback to user via GLib.idle_add

        except Exception as e:
            # Use GLib.idle_add to show error dialog in main thread if needed
            print(f"Error in Bandcamp import thread: {e}", file=sys.stderr)
            # TODO: Show feedback to user via GLib.idle_add

    # --- Playback Control Methods ---
    def _on_prev_clicked(self, button):
        """Handles the Previous button click."""
        if not self.player: return

        can_seek, position_ns = self.player.query_position(Gst.Format.TIME)
        state = self.player.get_state(0).state

        # If playing/paused and more than 3 seconds in, seek to beginning
        if state in (Gst.State.PLAYING, Gst.State.PAUSED) and can_seek and position_ns > (3 * Gst.SECOND):
            print("Previous: Seeking to beginning.")
            seek_flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT
            self.player.seek_simple(Gst.Format.TIME, seek_flags, 0)
        else:
            # Otherwise, go to the previous track in the list
            print("Previous: Selecting previous track.")
            current_pos = self.selection_model.get_selected()
            if current_pos != Gtk.INVALID_LIST_POSITION and current_pos > 0:
                self.selection_model.set_selected(current_pos - 1)
            # else: Already at the beginning or nothing selected, do nothing

    def _on_next_clicked(self, button=None): # Allow calling without button arg (from EOS)
        """Handles the Next button click or auto-plays next song."""
        print("Next: Selecting next track.")
        n_items = self.playlist_store.get_n_items()
        if n_items == 0: return # Nothing to play

        current_pos = self.selection_model.get_selected()

        if current_pos != Gtk.INVALID_LIST_POSITION and current_pos < (n_items - 1):
            self.selection_model.set_selected(current_pos + 1)
        elif current_pos == Gtk.INVALID_LIST_POSITION and n_items > 0:
             # If nothing selected, play the first song
             self.selection_model.set_selected(0)
        # else: Already at the end, do nothing (or loop?)

    # --- Playlist Open/Save Actions ---
    def _on_open_playlist_action(self, action, param): # <-- RESTORED SIGNATURE
        """Handles the 'win.open_playlist' action."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Open Playlist")
        dialog.set_modal(True)

        # Create filter for JSON files
        json_filter = Gtk.FileFilter.new()
        json_filter.set_name("Playlist Files (*.json)")
        json_filter.add_mime_type("application/json")
        json_filter.add_pattern("*.json")

        # Add filter to a ListStore for the dialog
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(json_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(json_filter) # Select JSON filter by default

        dialog.open(parent=self, cancellable=None, callback=self._on_open_dialog_finish)

    def _on_open_dialog_finish(self, dialog, result):
        """Callback after the open file dialog closes."""
        try:
            gio_file = dialog.open_finish(result)
            if gio_file:
                filepath = gio_file.get_path()
                print(f"Opening playlist from: {filepath}")
                # Clear existing playlist before loading
                self.playlist_store.remove_all()
                self._load_playlist(filepath=filepath)
        except GLib.Error as e:
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                print("Open playlist cancelled.")
            else:
                print(f"Error opening playlist file: {e.message}", file=sys.stderr)
                # Consider showing an error dialog to the user

    def _on_save_playlist_action(self, action, param): # <-- RESTORED SIGNATURE
        """Handles the 'win.save_playlist' action."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Save Playlist As")
        dialog.set_modal(True)
        dialog.set_initial_name("playlist.json")

        # Create filter for JSON files (same as open)
        json_filter = Gtk.FileFilter.new()
        json_filter.set_name("Playlist Files (*.json)")
        json_filter.add_mime_type("application/json")
        json_filter.add_pattern("*.json")

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(json_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(json_filter)

        # Suggest initial folder (e.g., Home directory)
        try:
            home_dir = GLib.get_home_dir()
            if home_dir:
                 initial_folder_file = Gio.File.new_for_path(home_dir)
                 dialog.set_initial_folder(initial_folder_file)
        except Exception as e:
            print(f"Could not set initial folder for save dialog: {e}")


        dialog.save(parent=self, cancellable=None, callback=self._on_save_dialog_finish)

    def _on_save_dialog_finish(self, dialog, result):
        """Callback after the save file dialog closes."""
        try:
            gio_file = dialog.save_finish(result)
            if gio_file:
                filepath = gio_file.get_path()
                # Ensure the filepath ends with .json
                if not filepath.lower().endswith(".json"):
                    filepath += ".json"
                    print(f"Appended .json extension. Saving to: {filepath}")
                else:
                    print(f"Saving playlist to: {filepath}")
                self._save_playlist(filepath=filepath)
        except GLib.Error as e:
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                print("Save playlist cancelled.")
            else:
                print(f"Error saving playlist file: {e.message}", file=sys.stderr)
                # Consider showing an error dialog to the user

    # --- Add Folder Action Handlers ---

    def _on_add_folder_action(self, action, param): # <-- RESTORED SIGNATURE
        """Handles the 'win.add_folder_new' action."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select Folder(s) to Add")
        dialog.set_modal(True)
        # No filters needed for folder selection

        print("Opening folder selection dialog...")
        dialog.select_multiple_folders(parent=self, cancellable=None,
                                     callback=self._on_select_multiple_folders_finish)

    def _on_select_multiple_folders_finish(self, dialog, result):
        """Callback after the select_multiple_folders dialog closes."""
        try:
            folders = dialog.select_multiple_folders_finish(result)
            if folders:
                n_folders = folders.get_n_items()
                print(f"Folders selected: {n_folders}")
                for i in range(n_folders):
                    folder_file = folders.get_item(i) # Gio.File object
                    if folder_file:
                        print(f"Processing selected folder: {folder_file.get_path()}")
                        self._start_folder_scan(folder_file) # Use existing scan logic
                    else:
                        print(f"Warning: Got null folder item at index {i}")
            else:
                # This case might occur if finish() returns None even without error (unlikely but possible)
                print("No folders selected or dialog closed unexpectedly.")

        except GLib.Error as e:
            # Handle errors, specifically checking for user cancellation
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                print("Folder selection cancelled.")
            else:
                # Treat other GLib errors as actual problems
                print(f"Error selecting folders: {e.message}", file=sys.stderr)
                # Consider showing an error dialog to the user
        except Exception as general_e: # Catch potential errors during finish() itself
             print(f"Unexpected error during folder selection finish: {general_e}", file=sys.stderr)

    # --- End Add Folder Action Handlers ---


    def _on_about_action(self, action, param): # <-- RESTORED SIGNATURE
        """Handles the 'win.about' action."""
        about_window = Adw.AboutWindow()
        about_window.set_transient_for(self)
        about_window.set_application_name("Namo Media Player")
        about_window.set_application_icon("audio-x-generic")
        about_window.set_version("0.1.0") # Placeholder
        about_window.set_developer_name("Elinor, with all my love.") # Using this for the text
        about_window.set_copyright("© 2025 Robert Renling for the Namo Project") # Placeholder copyleft
        about_window.set_developers(["Robert Renling", "hat tipped in the direction of Jorn Baayen."]) # Placeholder for Credits
        about_window.set_license_type(Gtk.License.CUSTOM) # Use CUSTOM for placeholder
        about_window.set_license("Namo is licensed under the GPL v2.") # Placeholder for Legal
        about_window.set_website("https://github.com/hardcoeur/Namo") # Placeholder website
        about_window.set_issue_url("https://github.com/hardcoeur/Namo/issues") # Placeholder issuetracker

        about_window.present()


    # --- Playlist Persistence ---
    def _load_playlist(self, filepath=None):
        """Loads the playlist from a JSON file. Uses default if filepath is None."""
        path_to_use = filepath if filepath else self._playlist_file_path

        if not os.path.exists(path_to_use):
            if filepath: # Only print error if a specific file was requested
                print(f"Error: Playlist file not found: {path_to_use}", file=sys.stderr)
            else:
                 print("Default playlist file not found, starting empty.")
            return

        print(f"Loading playlist from: {path_to_use}")
        try:
            with open(path_to_use, 'r') as f:
                playlist_data = json.load(f)

            if not isinstance(playlist_data, list):
                 print("Warning: Invalid playlist format (not a list). Starting empty.")
                 return

            for item in playlist_data:
                if isinstance(item, dict):
                     # Load duration (expecting nanoseconds integer >= 0 stored as 'duration_ns')
                     duration_ns_loaded = item.get('duration_ns')
                     if not isinstance(duration_ns_loaded, int) or duration_ns_loaded < 0:
                         duration_ns_loaded = 0

                     # Load and decode album art if present
                     album_art_glib_bytes = None
                     album_art_b64 = item.get('album_art_b64')
                     if album_art_b64:
                         try:
                             decoded_bytes = base64.b64decode(album_art_b64)
                             album_art_glib_bytes = GLib.Bytes.new(decoded_bytes)
                         except Exception as decode_e:
                             print(f"Error decoding album art for {item.get('title')}: {decode_e}")

                     song = Song(uri=item.get('uri'),
                                 title=item.get('title'),
                                 artist=item.get('artist'),
                                 duration=duration_ns_loaded)
                     # Assign loaded art data
                     if album_art_glib_bytes:
                         song.album_art_data = album_art_glib_bytes
                     self.playlist_store.append(song)
                else:
                     print(f"Warning: Skipping invalid item in playlist: {item}")

        except json.JSONDecodeError:
            print(f"Error: Could not decode playlist JSON from {path_to_use}. Starting empty.")
        except Exception as e:
            print(f"Error loading playlist from {path_to_use}: {e}", file=sys.stderr)

    def _save_playlist(self, filepath=None):
        """Saves the current playlist to a JSON file. Uses default if filepath is None."""
        path_to_use = filepath if filepath else self._playlist_file_path
        playlist_data = []
        for i in range(self.playlist_store.get_n_items()):
            song = self.playlist_store.get_item(i)
            # Ensure duration is int64 >= 0, default to 0 if not valid
            duration_to_save = song.duration if isinstance(song.duration, int) and song.duration >= 0 else 0

            # Create dictionary for JSON
            song_data_to_save = {
                'uri': song.uri,
                'title': song.title,
                'artist': song.artist,
                'duration_ns': duration_to_save # Use corrected duration
            }
            # Encode album art if present
            if song.album_art_data:
                 raw_bytes = song.album_art_data.get_data()
                 song_data_to_save['album_art_b64'] = base64.b64encode(raw_bytes).decode('ascii')

            # Append the final dictionary for this song
            playlist_data.append(song_data_to_save)

        try:
            # Ensure directory exists for the target path
            target_dir = os.path.dirname(path_to_use)
            if target_dir: # Only create if there's a directory part
                 os.makedirs(target_dir, exist_ok=True)

            print(f"Saving playlist to: {path_to_use}")
            with open(path_to_use, 'w') as f:
                json.dump(playlist_data, f, indent=2) # Use indent for readability

        except Exception as e:
            print(f"Error saving playlist to {path_to_use}: {e}", file=sys.stderr)

    # --- End Bandcamp Import Methods ---



class NamoApplication(Adw.Application):
    def __init__(self, **kwargs):
        super().__init__(application_id='com.example.NamoMediaPlayer',
                         flags=Gio.ApplicationFlags.FLAGS_NONE,
                        **kwargs)
        GLib.set_application_name("Namo Media Player")
        self.window = None

    def do_activate(self):
        # Activities within the application.
        if not self.window:
            self.window = NamoWindow(application=self)
        self.window.present()

    def do_startup(self):
        Adw.Application.do_startup(self)

        # Load CSS
        provider = Gtk.CssProvider()
        css_file = os.path.join(os.path.dirname(__file__), "style.css") # Path relative to namo.py
        if os.path.exists(css_file):
            provider.load_from_path(css_file)
            print(f"Loading CSS from: {css_file}")
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        else:
            print(f"Warning: style.css not found at {css_file}", file=sys.stderr)

    def do_shutdown(self):
        # Save playlist before cleaning up other resources
        if self.window:
             self.window._save_playlist()

        # Clean up resources
        if self.window and hasattr(self.window, 'discoverer') and self.window.discoverer:
            print("Stopping discoverer...")
            self.window.discoverer.stop()
        if self.window and hasattr(self.window, 'player') and self.window.player:
             print("Setting player to NULL state...")
             self.window.player.set_state(Gst.State.NULL)

        Adw.Application.do_shutdown(self)


def main():
    # Initialize GStreamer
    Gst.init(None)
    # Create a new application
    app = NamoApplication()
    return app.run(sys.argv)

if __name__ == "__main__":
    sys.exit(main())
