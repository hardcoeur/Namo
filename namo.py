#!/usr/bin/env python3

import sys
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gst', '1.0')
gi.require_version('GstPbutils', '1.0') 
gi.require_version('GdkPixbuf', '2.0')

import threading
import os 
import importlib.util 
import html 
import json 
import base64 
import mutagen
import pathlib 
from urllib.parse import urlparse, unquote
from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gst, GstPbutils, GdkPixbuf, Gdk, Pango 


bc_scraper = None
try:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    scraper_path = os.path.join(script_dir, "3rdp", "bandcamp-scraper.py")
    if os.path.exists(scraper_path):
        spec = importlib.util.spec_from_file_location("bc_scraper", scraper_path)
        if spec and spec.loader:
            bc_scraper = importlib.util.module_from_spec(spec)
            
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



class Song(GObject.Object):
    __gtype_name__ = 'Song'

    title = GObject.Property(type=str, default="Unknown Title")
    uri = GObject.Property(type=str) 
    artist = GObject.Property(type=str, default="Unknown Artist")
    duration = GObject.Property(type=GObject.TYPE_INT64, default=0) 
    album_art_data = GObject.Property(type=GLib.Bytes) 

    def __init__(self, uri, title=None, artist=None, duration=None):
        super().__init__()
        self.uri = uri
        self.title = title if title else "Unknown Title"
        self.artist = artist if artist else "Unknown Artist"
        
        self.duration = duration if isinstance(duration, int) and duration >= 0 else 0
        


class NamoWindow(Adw.ApplicationWindow):
    PLAY_ICON = "media-playback-start-symbolic"
    PAUSE_ICON = "media-playback-pause-symbolic"
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.current_song = None
        self._playlist_file_path = os.path.expanduser("~/.config/namo/playlist.json")
        self.duration_ns = 0 
        self._is_seeking = False 
        self._seek_value_ns = 0 
        self._was_playing_before_seek = False 
        self._init_player()
        self._setup_actions() 

        self.set_title("Namo Media Player")
        self.set_default_size(400, 800)

        
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        
        header = Adw.HeaderBar.new()
        toolbar_view.add_top_bar(header) 

        
        playback_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        playback_box.add_css_class("linked") 

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

        
        main_menu = Gio.Menu()
        
        main_menu.append("Open Playlist", "win.open_playlist")
        main_menu.append("Save Playlist", "win.save_playlist")
        main_menu.append("Add Folder...", "win.add_folder_new") 
        
        section = Gio.Menu()
        section.append("About", "win.about")
        main_menu.append_section(None, section)

        menu_button = Gtk.MenuButton.new()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_menu_model(main_menu) 

        
        add_button = Gtk.Button.new_from_icon_name("list-add-symbolic")
        add_button.connect("clicked", self._on_add_clicked)
        header.pack_end(menu_button) 
        header.pack_end(add_button)



        
        
        script_dir = os.path.dirname(os.path.abspath(__file__))
        
        bc_image_path = os.path.join(script_dir, "bcsymbol.png")
        
        bc_image = Gtk.Image.new_from_file(bc_image_path)
        
        import_bc_button = Gtk.Button()
        
        import_bc_button.set_child(bc_image)
        import_bc_button.set_tooltip_text("Import Bandcamp Album") 
        import_bc_button.connect("clicked", self._on_import_bandcamp_clicked)
        header.pack_end(import_bc_button) 


        
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        toolbar_view.set_content(main_box) 

        
        song_info_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        song_info_box.set_margin_start(12)
        song_info_box.set_margin_end(12)
        song_info_box.set_margin_top(6)
        song_info_box.set_margin_bottom(6)
        main_box.append(song_info_box)

        self.cover_image = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic") 
        self.cover_image.set_pixel_size(64)
        self.cover_image.add_css_class("album-art-image") 
        song_info_box.append(self.cover_image)

        song_details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        song_details_box.set_valign(Gtk.Align.CENTER)
        song_info_box.append(song_details_box)

        self.song_label = Gtk.Label(label="<Song Title>", xalign=0)
        self.song_label.set_ellipsize(Pango.EllipsizeMode.END) 
        self.song_label.add_css_class("title-5") 
        song_details_box.append(self.song_label)

        self.time_label = Gtk.Label(label="0:00 / 0:00", xalign=0)
        self.time_label.add_css_class("caption") 
        song_details_box.append(self.time_label)

        
        self.progress_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.progress_scale.set_draw_value(False) 
        self.progress_scale.set_hexpand(True)
        self.progress_scale.set_sensitive(False) 
        
        main_box.append(self.progress_scale)

        
        drag_controller = Gtk.GestureDrag()
        drag_controller.connect("drag-begin", self._on_seek_drag_begin)
        drag_controller.connect("drag-update", self._on_seek_drag_update)
        drag_controller.connect("drag-end", self._on_seek_drag_end)
        self.progress_scale.add_controller(drag_controller)


        
        playlist_header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        playlist_header_box.set_margin_start(12) 
        playlist_header_box.set_margin_end(12)

        playlist_label = Gtk.Label(label="Playlist", xalign=0, hexpand=True) 
        playlist_label.add_css_class("title-4")
        playlist_header_box.append(playlist_label)

        self.remaining_time_label = Gtk.Label(label="", xalign=1, halign=Gtk.Align.END) 
        self.remaining_time_label.add_css_class("caption") 
        playlist_header_box.append(self.remaining_time_label)

        main_box.append(playlist_header_box) 

        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_vexpand(True)
        scrolled_window.set_hexpand(False)
        scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        main_box.append(scrolled_window)

        
        self.playlist_store = Gio.ListStore(item_type=Song) 
        self.playlist_store.connect("items-changed", lambda store, pos, rem, add: self._update_remaining_time()) 

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_playlist_item_setup)
        factory.connect("bind", self._on_playlist_item_bind)

        self.selection_model = Gtk.SingleSelection(model=self.playlist_store)
        self.selection_model.connect("selection-changed", self._on_playlist_selection_changed)
        self.selection_model.connect("selection-changed", lambda sel, pos, n_items: self._update_remaining_time()) 

        self.playlist_view = Gtk.ListView(model=self.selection_model,
                                          factory=factory)
        self.playlist_view.set_vexpand(True)
        self.playlist_view.set_vexpand(False)
        scrolled_window.set_child(self.playlist_view)

        
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_playlist_key_pressed)
        self.playlist_view.add_controller(key_controller)

        
        self._load_playlist()
        self._update_remaining_time() 

    
    def _setup_actions(self):
        action_group = Gio.SimpleActionGroup()

        open_action = Gio.SimpleAction.new("open_playlist", None)
        open_action.connect("activate", self._on_open_playlist_action)
        action_group.add_action(open_action)

        save_action = Gio.SimpleAction.new("save_playlist", None)
        save_action.connect("activate", self._on_save_playlist_action)
        action_group.add_action(save_action)

        add_folder_action = Gio.SimpleAction.new("add_folder_new", None) 
        add_folder_action.connect("activate", self._on_add_folder_action)
        action_group.add_action(add_folder_action)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about_action)
        action_group.add_action(about_action)

        self.insert_action_group("win", action_group)

    def _init_player(self):
        """Initialize GStreamer player and discoverer."""
        
        self.discoverer = GstPbutils.Discoverer.new(5 * Gst.SECOND) 
        self.discoverer.connect("discovered", self._on_discoverer_discovered)
        self.discoverer.connect("finished", self._on_discoverer_finished)
        self.discoverer.start() 

        
        self.player = Gst.ElementFactory.make("playbin", "player")
        if not self.player:
            print("ERROR: Could not create GStreamer playbin element.", file=sys.stderr)
            
            return
        
        rgvolume = Gst.ElementFactory.make("rgvolume", "rgvolume")
        if not rgvolume:
            print("ERROR: Could not create rgvolume element.", file=sys.stderr)
            return

        
        self.player.set_property("audio-filter", rgvolume)

        
        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_player_message)

    def play_uri(self, uri):
        """Loads and starts playing a URI."""
        if not self.player:
            self.current_song = None
            return

        
        self.current_song = None
        for i in range(self.playlist_store.get_n_items()):
             song = self.playlist_store.get_item(i)
             if song.uri == uri:
                 self.current_song = song
                 break

        self._update_song_display(self.current_song)

        print(f"Playing URI: {uri}")
        self.player.set_property("uri", uri)
        self.player.set_state(Gst.State.PLAYING)
        
        self.play_pause_button.set_icon_name(self.PAUSE_ICON)
        
        self.duration_ns = 0 
        self.progress_scale.set_value(0) 
        

    def toggle_play_pause(self, button=None):
        """Toggles playback state."""
        if not self.player: return

        state = self.player.get_state(0).state
        if state == Gst.State.PLAYING:
            print("Pausing playback")
            self.player.set_state(Gst.State.PAUSED)
            self.play_pause_button.set_icon_name(self.PLAY_ICON)
        elif state == Gst.State.PAUSED or state == Gst.State.READY:
             
            print("Resuming/Starting playback")
            self.player.set_state(Gst.State.PLAYING)
            self.play_pause_button.set_icon_name(self.PAUSE_ICON)
        elif state == Gst.State.NULL:
            
            print("No media loaded to play.")
            
            pass
        


    def _on_playlist_item_setup(self, factory, list_item):
        """Setup widgets for a song row."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.set_margin_top(3)
        box.set_margin_bottom(3)

        title_label = Gtk.Label(xalign=0, hexpand=True)
        title_label.set_ellipsize(Pango.EllipsizeMode.END) 
        title_label.set_tooltip_text("") 
        title_label.set_max_width_chars(40)
        duration_label = Gtk.Label(xalign=1, hexpand=False)
        box.append(title_label)
        box.append(duration_label)
        list_item.set_child(box)

        
        gesture = Gtk.GestureClick.new()
        box.add_controller(gesture)
        
        list_item._click_gesture = gesture



    def _on_playlist_item_bind(self, factory, list_item):
        """Bind song data to the widgets."""
        box = list_item.get_child()
        title_label = box.get_first_child()
        duration_label = box.get_last_child()

        song = list_item.get_item() 

        full_title = f"{song.artist} - {song.title}"
        title_label.set_label(full_title)
        title_label.set_tooltip_text(full_title) 
        
        
        duration_ns = song.duration
        if duration_ns != Gst.CLOCK_TIME_NONE and duration_ns > 0:
             seconds = duration_ns // Gst.SECOND
             duration_str = f"{seconds // 60}:{seconds % 60:02d}"
        else:
             duration_str = "--:--" 

        
        duration_label.set_label(duration_str)

        
        
        
        
        gesture = getattr(list_item, "_click_gesture", None)
        if gesture and isinstance(gesture, Gtk.GestureClick):
            
            handler_id = getattr(list_item, "_click_handler_id", None)
            if handler_id:
                try:
                    gesture.disconnect(handler_id)
                except TypeError: 
                    print(f"Warning: Failed to disconnect handler {handler_id}, might be already disconnected.")

            
            new_handler_id = gesture.connect("pressed", self._on_song_row_activated, song)
            list_item._click_handler_id = new_handler_id
        else:
             print("Warning: Could not find click_gesture on list item during bind.")

    def _on_song_row_activated(self, gesture, n_press, x, y, song):
        """Handles activation (double-click) on a playlist row."""
        
        if n_press == 2:
            print(f"Double-clicked/Activated song: {song.title}")
            if song and song.uri:
                 
                 if self.player:
                     print("Stopping current playback due to activation.")
                     self.player.set_state(Gst.State.NULL)
                 
                 self.play_uri(song.uri)
            else:
                 print("Cannot play activated item (no URI?).")


    def _on_playlist_selection_changed(self, selection_model, position, n_items):
        """Callback when the selected song in the playlist changes."""
        selected_item = selection_model.get_selected_item()
        if selected_item:
            print(f"Selected: {selected_item.artist} - {selected_item.title}")
            
            self._update_song_display(selected_item)
        else:
            
            self._update_song_display(None)

    def _on_playlist_key_pressed(self, controller, keyval, keycode, state):
        """Handles key presses on the playlist view, specifically Delete/Backspace."""
        if keyval == Gdk.KEY_Delete or keyval == Gdk.KEY_BackSpace:
            position = self.selection_model.get_selected()
            if position != Gtk.INVALID_LIST_POSITION:
                print(f"Deleting item at position: {position}")
                self.playlist_store.remove(position)
                
                
                return True 
        return False 

    def _on_add_clicked(self, button):
        """Handles the Add button click: shows a Gtk.FileDialog."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Add Music Files")
        dialog.set_modal(True)
        
        
        
        
        
        
        

        dialog.open_multiple(parent=self, cancellable=None,
                             callback=self._on_file_dialog_open_multiple_finish)

    def _on_file_dialog_open_multiple_finish(self, dialog, result):
        """Handles the response from the Gtk.FileDialog."""
        try:
            files = dialog.open_multiple_finish(result)
            if files:
                print(f"Processing {files.get_n_items()} selected items...")
                for i in range(files.get_n_items()):
                    gio_file = files.get_item(i) 
                    if not gio_file: continue

                    try:
                        
                        info = gio_file.query_info(
                            Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
                            Gio.FileQueryInfoFlags.NONE,
                            None
                        )
                        file_type = info.get_file_type()

                        if file_type == Gio.FileType.REGULAR:
                            print(f"Adding regular file: {gio_file.get_uri()}")
                            self._discover_and_add_uri(gio_file.get_uri()) 
                        elif file_type == Gio.FileType.DIRECTORY:
                            print(f"Starting scan for directory: {gio_file.get_path()}")
                            self._start_folder_scan(gio_file) 
                        else:
                            print(f"Skipping unsupported file type: {gio_file.get_path()}")

                    except GLib.Error as info_err:
                         print(f"Error querying info for {gio_file.peek_path()}: {info_err.message}", file=sys.stderr)
                    except Exception as proc_err: 
                         print(f"Error processing item {gio_file.peek_path()}: {proc_err}", file=sys.stderr)

        except GLib.Error as e:
            
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                print("File selection cancelled.")
            else:
                
                print(f"Error opening files: {e.message}", file=sys.stderr)
        except Exception as general_e: 
             print(f"Unexpected error during file dialog finish: {general_e}", file=sys.stderr)

    def _start_folder_scan(self, folder_gio_file):
        """Starts a background thread to scan a folder for audio files."""
        
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
                            
                            song_object = self._discover_uri_sync(file_uri, full_path)

                            if song_object:
                                files_added += 1
                                
                                GLib.idle_add(self.playlist_store.append, song_object)
                                
                                
                                
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
        
        mutagen_title = None
        mutagen_artist = None
        album_art_bytes = None
        album_art_glib_bytes = None
        duration_ns = 0 

        try:
            if not os.path.exists(filepath):
                 print(f"Sync Discover Error: File path does not exist: {filepath}", file=sys.stderr)
                 return None

            
            try:
                
                audio_easy = mutagen.File(filepath, easy=True)
                if audio_easy:
                    mutagen_title = audio_easy.get('title', [None])[0]
                    mutagen_artist = audio_easy.get('artist', [None])[0]
                    
                    duration_str = audio_easy.get('length', [None])[0]
                    if duration_str:
                        try: duration_ns = int(float(duration_str) * Gst.SECOND)
                        except (ValueError, TypeError): pass 
            except Exception as tag_e:
                print(f"Sync Discover: Mutagen error reading easy tags from {filepath}: {tag_e}", file=sys.stderr)

            
            try:
                audio_raw = mutagen.File(filepath)
                if audio_raw:
                    
                    if duration_ns <= 0 and audio_raw.info and hasattr(audio_raw.info, 'length'):
                        try: duration_ns = int(audio_raw.info.length * Gst.SECOND)
                        except (ValueError, TypeError): pass

                    
                    if audio_raw.tags:
                        if isinstance(audio_raw.tags, mutagen.id3.ID3) and 'APIC:' in audio_raw.tags:
                            album_art_bytes = audio_raw.tags['APIC:'].data
                        elif isinstance(audio_raw, mutagen.mp4.MP4) and 'covr' in audio_raw.tags and audio_raw.tags['covr']:
                            album_art_bytes = bytes(audio_raw.tags['covr'][0])
                        elif hasattr(audio_raw, 'pictures') and audio_raw.pictures:
                            album_art_bytes = audio_raw.pictures[0].data

                    
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
            

        
        
        final_title = mutagen_title if mutagen_title else os.path.splitext(os.path.basename(filepath))[0]
        final_artist = mutagen_artist 
        
        duration_to_store = duration_ns if isinstance(duration_ns, int) and duration_ns >= 0 else 0

        
        try:
            song = Song(uri=uri, title=final_title, artist=final_artist, duration=duration_to_store)

            
            if album_art_glib_bytes:
                 song.album_art_data = album_art_glib_bytes

            
            return song
        except Exception as song_create_e:
             print(f"Sync Discover: Error creating Song object for {filepath}: {song_create_e}", file=sys.stderr)
             return None 

    
    def _discover_and_add_uri(self, uri):
        
        print(f"Starting ASYNC discovery for: {uri}") 
        self.discoverer.discover_uri_async(uri)

    def _on_discoverer_discovered(self, discoverer, info, error):
        """Callback when GstDiscoverer finishes discovering a URI."""
        
        print(f"--- ASYNC _on_discoverer_discovered called for URI: {info.get_uri()} ---") 
        uri = info.get_uri()

        
        if error:
            print(f"Error discovering URI: {uri} - {error.message}")
            if error.matches(Gst.DiscovererError.quark(), Gst.DiscovererError.URI_INVALID):
                print("Invalid URI.")
            elif error.matches(Gst.DiscovererError.quark(), Gst.DiscovererError.MISSING_PLUGIN):
                caps_struct = error.get_details() 
                if caps_struct:
                    print(f"Missing decoder for: {caps_struct.to_string()}")
                else:
                    print("Missing decoder details unavailable.")
            elif error.matches(Gst.DiscovererError.quark(), Gst.DiscovererError.MISC):
                print(f"Misc error: {error.message}")
            return 

        
        result = info.get_result()
        if result == GstPbutils.DiscovererResult.OK:
            gst_tags = info.get_tags()
            duration_ns = info.get_duration()

            
            mutagen_title = None
            mutagen_artist = None
            album_art_bytes = None
            album_art_glib_bytes = None 

            
            if uri.startswith('file://'):
                try:
                    parsed_uri = urlparse(uri)
                    
                    filepath_parts = [parsed_uri.netloc, unquote(parsed_uri.path)]
                    filepath = os.path.abspath(os.path.join(*filter(None, filepath_parts)))

                    if not os.path.exists(filepath):
                         print(f"Mutagen error: File path does not exist: {filepath}", file=sys.stderr)
                    else:
                        print(f"Attempting to read tags/art with Mutagen: {filepath}")
                        
                        try:
                            audio_easy = mutagen.File(filepath, easy=True)
                            if audio_easy:
                                mutagen_title = audio_easy.get('title', [None])[0]
                                mutagen_artist = audio_easy.get('artist', [None])[0]
                        except Exception as tag_e:
                            print(f"Mutagen error reading easy tags from {filepath}: {tag_e}", file=sys.stderr)

                        
                        try:
                            print(f"Attempting Mutagen raw file load: {filepath}")
                            audio_raw = mutagen.File(filepath)
                            if audio_raw and audio_raw.tags:
                                print(f"Mutagen raw tags found: Type={type(audio_raw.tags)}")
                                if isinstance(audio_raw.tags, mutagen.id3.ID3): 
                                    if 'APIC:' in audio_raw.tags:
                                        print("Found APIC tag in ID3.")
                                        album_art_bytes = audio_raw.tags['APIC:'].data
                                    else:
                                        print("No APIC tag found in ID3.")
                                elif isinstance(audio_raw, mutagen.mp4.MP4): 
                                    if 'covr' in audio_raw.tags:
                                        artworks = audio_raw.tags['covr']
                                        if artworks:
                                            print("Found covr tag in MP4.")
                                            album_art_bytes = bytes(artworks[0])
                                        else:
                                            print("covr tag found but empty in MP4.")
                                    else:
                                        print("No covr tag found in MP4.")
                                elif hasattr(audio_raw, 'pictures') and audio_raw.pictures: 
                                    print(f"Found {len(audio_raw.pictures)} pictures in tags.")
                                    album_art_bytes = audio_raw.pictures[0].data
                                else:
                                    print("No known picture tag (APIC, covr, pictures) found.")

                                
                                if album_art_bytes:
                                    try:
                                        print(f"Attempting to create GLib.Bytes from art ({len(album_art_bytes)} bytes).")
                                        album_art_glib_bytes = GLib.Bytes.new(album_art_bytes)
                                    except Exception as wrap_e:
                                        print(f"Error wrapping album art bytes: {wrap_e}", file=sys.stderr)
                                        album_art_glib_bytes = None 
                            else:
                                print(f"Mutagen could not find tags in raw file: {filepath}")

                        except Exception as art_e:
                             print(f"Mutagen error reading raw file/art tags from {filepath}: {art_e}", file=sys.stderr)

                except Exception as e:
                    print(f"General Mutagen error for {uri}: {e}", file=sys.stderr)

            
            gst_title = gst_tags.get_string(Gst.TAG_TITLE)[1] if gst_tags and gst_tags.get_string(Gst.TAG_TITLE)[0] else None
            gst_artist = gst_tags.get_string(Gst.TAG_ARTIST)[1] if gst_tags and gst_tags.get_string(Gst.TAG_ARTIST)[0] else None

            
            final_title = mutagen_title if mutagen_title is not None else gst_title
            final_artist = mutagen_artist if mutagen_artist is not None else gst_artist
            duration_to_store = duration_ns if isinstance(duration_ns, int) and duration_ns >= 0 else 0

            
            song_to_add = Song(uri=uri, title=final_title, artist=final_artist, duration=duration_to_store)

            
            if album_art_glib_bytes: 
                 try:
                     song_to_add.album_art_data = album_art_glib_bytes
                     print(f"Successfully assigned album art data to Song object for {final_title}")
                 except Exception as assign_e:
                      print(f"Error assigning album art GLib.Bytes: {assign_e}", file=sys.stderr)

            
            print(f"Discovered OK: URI='{song_to_add.uri}', Title='{song_to_add.title}', Artist='{song_to_add.artist}', Duration={song_to_add.duration / Gst.SECOND:.2f}s, Art Assigned={song_to_add.album_art_data is not None}")

            
            GLib.idle_add(self.playlist_store.append, song_to_add)
            print(f"Scheduled add for: {final_title or 'Unknown Title'}")

        elif result == GstPbutils.DiscovererResult.TIMEOUT:
             print(f"Discovery Timeout: {uri}", file=sys.stderr)
        elif result == GstPbutils.DiscovererResult.BUSY:
             print(f"Discovery Busy: {uri} - Retrying later?", file=sys.stderr)
        elif result == GstPbutils.DiscovererResult.MISSING_PLUGINS:
             print(f"Discovery Missing Plugins: {uri}", file=sys.stderr)
             
        else:
             print(f"Discovery Result: {uri} - {result}", file=sys.stderr)


    def _on_discoverer_finished(self, discoverer):
        print("--- _on_discoverer_finished called ---") 

    def _on_player_message(self, bus, message):
        """Handles messages from the GStreamer bus."""
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(f"ERROR: {err.message} ({dbg})", file=sys.stderr)
            
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
                self.player.set_state(Gst.State.NULL) 
                self.play_pause_button.set_icon_name(self.PLAY_ICON)
                self.progress_scale.set_value(0)
                self.progress_scale.set_sensitive(False)
                self.time_label.set_label("0:00 / 0:00")
                self.song_label.set_label("<No Song Playing>")
                self.current_song = None
                
                print("EOS: Selecting next song.")
                self._on_next_clicked() 

                
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
            
            if message.src == self.player:
                print(f"State changed from {old_state.value_nick} to {new_state.value_nick}")
                if new_state == Gst.State.PLAYING:
                    self.play_pause_button.set_icon_name(self.PAUSE_ICON)
                    
                    
                    if not hasattr(self, '_progress_timer_id') or self._progress_timer_id is None:
                         self._progress_timer_id = GLib.timeout_add_seconds(1, self._update_progress)
                    
                    
                         self._progress_timer_id = GLib.timeout_add_seconds(1, self._update_progress)
                elif new_state == Gst.State.PAUSED:
                    self.play_pause_button.set_icon_name(self.PLAY_ICON)
                    
                elif new_state == Gst.State.READY or new_state == Gst.State.NULL:
                    self.play_pause_button.set_icon_name(self.PLAY_ICON)
                    self.progress_scale.set_value(0)
                    
                    
                    if hasattr(self, '_progress_timer_id') and self._progress_timer_id is not None:
                        GLib.source_remove(self._progress_timer_id)
                        self._progress_timer_id = None
        elif t == Gst.MessageType.DURATION_CHANGED:
             
             self.duration_ns = self.player.query_duration(Gst.Format.TIME)[1]
             print(f"Duration changed: {self.duration_ns / Gst.SECOND:.2f}s")
             if self.duration_ns > 0:
                 self.progress_scale.set_range(0, self.duration_ns / Gst.SECOND)
                 self.progress_scale.set_sensitive(True) 
             else:
                 self.progress_scale.set_range(0, 0) 
                 self.progress_scale.set_sensitive(False) 
             GLib.idle_add(self._update_progress) 


        
        return True
    def _update_song_display(self, song):
        """Updates the song title, artist, time label (0:00 / Duration), and cover art."""
        if song:
            
            self.song_label.set_label(f"{song.artist} - {song.title}")
            self.song_label.set_tooltip_text(f"{song.artist} - {song.title}") 

            
            duration_ns = song.duration
            if duration_ns is not None and duration_ns != Gst.CLOCK_TIME_NONE and duration_ns > 0:
                dur_sec = duration_ns // Gst.SECOND
                duration_str = f"{dur_sec // 60}:{dur_sec % 60:02d}"
            else:
                duration_str = "--:--"
            self.time_label.set_label(f"0:00 / {duration_str}")

            
            glib_bytes_data = song.album_art_data
            
            if glib_bytes_data:
                raw_bytes_data = glib_bytes_data.get_data() 
                try:
                    loader = GdkPixbuf.PixbufLoader()
                    loader.write(raw_bytes_data)
                    loader.close()
                    pixbuf = loader.get_pixbuf()
                    
                    scaled_pixbuf = pixbuf.scale_simple(64, 64, GdkPixbuf.InterpType.BILINEAR)
                    self.cover_image.set_from_pixbuf(scaled_pixbuf)
                    
                except Exception as e:
                    print(f"Error loading album art in _update_song_display for '{song.title}': {e}", file=sys.stderr)
                    self.cover_image.set_from_icon_name("audio-x-generic-symbolic") 
            else:
                
                self.cover_image.set_from_icon_name("audio-x-generic-symbolic") 
        else:
            
            
            self.song_label.set_label("<No Song Playing>")
            self.song_label.set_tooltip_text("") 
            self.time_label.set_label("0:00 / 0:00")
            self.cover_image.set_from_icon_name("audio-x-generic-symbolic")


    
    def _update_remaining_time(self):
        """Calculates and updates the remaining playlist time label."""
        total_remaining_ns = 0
        selected_index = self.selection_model.get_selected()

        
        
        start_index = 0
        if selected_index != Gtk.INVALID_LIST_POSITION:
            start_index = selected_index + 1

        n_items = self.playlist_store.get_n_items()
        for i in range(start_index, n_items):
            song = self.playlist_store.get_item(i)
            
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
            elif total_seconds > 0: 
                 formatted_string = "<1m remaining"
            

        
        GLib.idle_add(self.remaining_time_label.set_text, formatted_string)

    

    def _update_progress(self):
        
        if self._is_seeking:
            
            return True 
        """Timer callback to update playback progress."""
        if not self.player or not self.current_song:
            
            if hasattr(self, '_progress_timer_id') and self._progress_timer_id is not None:
                GLib.source_remove(self._progress_timer_id)
                self._progress_timer_id = None
            return False 

        state = self.player.get_state(0).state
        if state != Gst.State.PLAYING and state != Gst.State.PAUSED:
            
            self._progress_timer_id = None
            return False

        
        
        if self.duration_ns <= 0:
             ok, new_duration_ns = self.player.query_duration(Gst.Format.TIME)
             if ok:
                 self.duration_ns = new_duration_ns 
             else:
                 print("Could not query duration in timer.")
                 self.duration_ns = 0 
        
        ok_pos, position_ns = self.player.query_position(Gst.Format.TIME)
        pos_sec = 0 
        if ok_pos:
            pos_sec = position_ns // Gst.SECOND 
            
            
            label_text = "" 
            if self.duration_ns > 0:
                 dur_sec = self.duration_ns // Gst.SECOND
                 label_text = f"{pos_sec // 60}:{pos_sec % 60:02d} / {dur_sec // 60}:{dur_sec % 60:02d}"
            else:
                 label_text = f"{pos_sec // 60}:{pos_sec % 60:02d} / --:--"

            self.time_label.set_label(label_text) 

            
            adj = self.progress_scale.get_adjustment()
            adj.set_value(pos_sec)


        
        
        if state == Gst.State.PLAYING:
            return True 
        else:
            self._progress_timer_id = None 
            return False 


    

    def _on_seek_drag_begin(self, gesture, start_x, start_y):
        """Called when the user starts dragging the progress scale."""
        print("Seek drag begin")
        self._is_seeking = True
        
        self._was_playing_before_seek = (self.player.get_state(0).state == Gst.State.PLAYING)
        print(f"DEBUG: Drag begin. Was playing: {self._was_playing_before_seek}") 
        
        if hasattr(self, '_progress_timer_id') and self._progress_timer_id is not None:
            print("Stopping progress timer for seek")
            GLib.source_remove(self._progress_timer_id)
            self._progress_timer_id = None

    def _on_seek_drag_update(self, gesture, offset_x, offset_y):
        """Called continuously while the user drags the progress scale."""
        if not self._is_seeking: return 

        adj = self.progress_scale.get_adjustment()
        lower = adj.get_lower()
        upper = adj.get_upper()
        alloc = self.progress_scale.get_allocation()

        if alloc.width == 0: return 

        start_x, _ = gesture.get_start_point()
        
        target_value = lower + (upper - lower) * (start_x + offset_x) / alloc.width
        
        target_value = max(lower, min(target_value, upper))

        

        
        

        
        self._seek_value_ns = int(target_value * Gst.SECOND)

        
        pos_sec = int(target_value)
        if self.duration_ns > 0:
             dur_sec = self.duration_ns // Gst.SECOND
             self.time_label.set_label(f"{pos_sec // 60}:{pos_sec % 60:02d} / {dur_sec // 60}:{dur_sec % 60:02d}")
        else:
             
             self.time_label.set_label(f"{pos_sec // 60}:{pos_sec % 60:02d} / --:--")


    def _on_seek_drag_end(self, gesture, offset_x, offset_y):
        """Called when the user releases the progress scale after dragging."""
        print(f"Seek drag end: offset_x={offset_x:.2f}")
        if not self._is_seeking: return 

        self._is_seeking = False

        
        if self.player and self.duration_ns > 0:
            print(f"Performing seek to: {self._seek_value_ns / Gst.SECOND:.2f}s")
            seek_flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT
            if not self.player.seek_simple(Gst.Format.TIME, seek_flags, self._seek_value_ns):
                print("Seek failed.", file=sys.stderr)
            else:
                
                final_seek_pos_sec = self._seek_value_ns / Gst.SECOND
                self.progress_scale.set_value(final_seek_pos_sec)

            
            
            
            
            state = self.player.get_state(0).state
            if state == Gst.State.PLAYING:
                 if not hasattr(self, '_progress_timer_id') or self._progress_timer_id is None:
                     
                     print("Scheduling delayed timer restart after seek.")
                     GLib.timeout_add(100, self._restart_progress_timer) 
            else:
                 print("Not scheduling timer restart (player was not playing before seek)")
        else:
            print("Not seeking: Player invalid or duration unknown.")
    
    def _restart_progress_timer(self):
        if not hasattr(self, '_progress_timer_id') or self._progress_timer_id is None:
             
             if self._was_playing_before_seek:
                 print("Restarting progress timer after seek delay (was playing before).")
                 self._progress_timer_id = GLib.timeout_add_seconds(1, self._update_progress)
             else:
                 print("Not restarting progress timer (was not playing before seek).")
                 self._progress_timer_id = GLib.timeout_add_seconds(1, self._update_progress)
        return GLib.SOURCE_REMOVE 


    
    def _on_import_bandcamp_clicked(self, button):
        """Shows a dialog to get the Bandcamp album URL."""
        if not bc_scraper:
            print("Bandcamp scraper not available.")
            
            return

        dialog = Adw.MessageDialog.new(self,
                                       "Import Bandcamp Album",
                                       "Please enter the full URL of the Bandcamp album:")
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("ok", "_Import")
        dialog.set_default_response("ok")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        
        url_entry = Gtk.Entry()
        url_entry.set_placeholder_text("https://artist.bandcamp.com/album/album-name")
        url_entry.set_activates_default(True) 
        dialog.set_extra_child(url_entry) 

        dialog.connect("response", self._on_bandcamp_dialog_response, url_entry)
        dialog.present()


    def _on_bandcamp_dialog_response(self, dialog, response_id, url_entry):
        """Handles the response from the Bandcamp URL dialog."""
        if response_id == "ok":
            url = url_entry.get_text().strip()
            if url:
                
                if url.startswith("http://") or url.startswith("https://"):
                     print(f"Starting Bandcamp import for: {url}")
                     self._start_bandcamp_import(url)
                else:
                     print("Invalid URL entered.")
                     
            else:
                 print("No URL entered.")
        else:
            print("Bandcamp import cancelled.")
        

    def _start_bandcamp_import(self, url):
        
        print(f"Scheduling import thread for {url}")
        thread = threading.Thread(target=self._run_bandcamp_import_thread, args=(url,), daemon=True)
        thread.start()

    def _run_bandcamp_import_thread(self, url):
        if not bc_scraper: return 

        print(f"Background thread started for: {url}")
        try:
            
            track_infos = bc_scraper.get_album_track_info(url)
            if not track_infos:
                print("No tracks found or error during scraping.")
                
                return

            print(f"Scraped {len(track_infos)} tracks. Adding to playlist...")
            for info in track_infos:
                
                duration_str = "--:--"
                if info.get("duration"):
                    try:
                        seconds = int(float(info["duration"])) 
                        duration_str = f"{seconds // 60}:{seconds % 60:02d}"
                    except (ValueError, TypeError):
                        pass 

                
                
                stream_url = info.get("stream_url")
                if not stream_url:
                    print(f"Skipping track '{info.get('title')}' - no stream URL found.")
                    continue

                
                title = html.unescape(info.get("title", "Unknown Title"))
                artist = html.unescape(info.get("artist", "Unknown Artist"))

                
                duration_ns_bc = 0
                if info.get("duration"):
                    try:
                        temp_ns = int(float(info["duration"]) * Gst.SECOND)
                        if temp_ns >= 0:
                             duration_ns_bc = temp_ns
                    except (ValueError, TypeError):
                        pass 

                song = Song(uri=stream_url,
                            title=title,
                            artist=artist,
                            duration=duration_ns_bc) 

                
                GLib.idle_add(self.playlist_store.append, song)
                
                

            print("Finished adding Bandcamp tracks to playlist.")
            

        except Exception as e:
            
            print(f"Error in Bandcamp import thread: {e}", file=sys.stderr)
            

    
    def _on_prev_clicked(self, button):
        """Handles the Previous button click."""
        if not self.player: return

        can_seek, position_ns = self.player.query_position(Gst.Format.TIME)
        state = self.player.get_state(0).state

        
        if state in (Gst.State.PLAYING, Gst.State.PAUSED) and can_seek and position_ns > (3 * Gst.SECOND):
            print("Previous: Seeking to beginning.")
            seek_flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT
            self.player.seek_simple(Gst.Format.TIME, seek_flags, 0)
        else:
            
            print("Previous: Selecting previous track.")
            current_pos = self.selection_model.get_selected()
            if current_pos != Gtk.INVALID_LIST_POSITION and current_pos > 0:
                self.selection_model.set_selected(current_pos - 1)
            

    def _on_next_clicked(self, button=None): 
        """Handles the Next button click or auto-plays next song."""
        print("Next: Selecting next track.")
        n_items = self.playlist_store.get_n_items()
        if n_items == 0: return 

        current_pos = self.selection_model.get_selected()

        if current_pos != Gtk.INVALID_LIST_POSITION and current_pos < (n_items - 1):
            self.selection_model.set_selected(current_pos + 1)
        elif current_pos == Gtk.INVALID_LIST_POSITION and n_items > 0:
             
             self.selection_model.set_selected(0)
        

    
    def _on_open_playlist_action(self, action, param): 
        """Handles the 'win.open_playlist' action."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Open Playlist")
        dialog.set_modal(True)

        
        json_filter = Gtk.FileFilter.new()
        json_filter.set_name("Playlist Files (*.json)")
        json_filter.add_mime_type("application/json")
        json_filter.add_pattern("*.json")

        
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(json_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(json_filter) 

        dialog.open(parent=self, cancellable=None, callback=self._on_open_dialog_finish)

    def _on_open_dialog_finish(self, dialog, result):
        """Callback after the open file dialog closes."""
        try:
            gio_file = dialog.open_finish(result)
            if gio_file:
                filepath = gio_file.get_path()
                print(f"Opening playlist from: {filepath}")
                
                self.playlist_store.remove_all()
                self._load_playlist(filepath=filepath)
        except GLib.Error as e:
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                print("Open playlist cancelled.")
            else:
                print(f"Error opening playlist file: {e.message}", file=sys.stderr)
                

    def _on_save_playlist_action(self, action, param): 
        """Handles the 'win.save_playlist' action."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Save Playlist As")
        dialog.set_modal(True)
        dialog.set_initial_name("playlist.json")

        
        json_filter = Gtk.FileFilter.new()
        json_filter.set_name("Playlist Files (*.json)")
        json_filter.add_mime_type("application/json")
        json_filter.add_pattern("*.json")

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(json_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(json_filter)

        
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
                

    

    def _on_add_folder_action(self, action, param): 
        """Handles the 'win.add_folder_new' action."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select Folder(s) to Add")
        dialog.set_modal(True)
        

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
                    folder_file = folders.get_item(i) 
                    if folder_file:
                        print(f"Processing selected folder: {folder_file.get_path()}")
                        self._start_folder_scan(folder_file) 
                    else:
                        print(f"Warning: Got null folder item at index {i}")
            else:
                
                print("No folders selected or dialog closed unexpectedly.")

        except GLib.Error as e:
            
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                print("Folder selection cancelled.")
            else:
                
                print(f"Error selecting folders: {e.message}", file=sys.stderr)
                
        except Exception as general_e: 
             print(f"Unexpected error during folder selection finish: {general_e}", file=sys.stderr)

    


    def _on_about_action(self, action, param): 
        """Handles the 'win.about' action."""
        about_window = Adw.AboutWindow()
        about_window.set_transient_for(self)
        about_window.set_application_name("Namo Media Player")
        about_window.set_application_icon("audio-x-generic")
        about_window.set_version("0.1.0") 
        about_window.set_developer_name("Elinor, with all my love.") 
        about_window.set_copyright("© 2025 Robert Renling for the Namo Project") 
        about_window.set_developers(["Robert Renling", "hat tipped in the direction of Jorn Baayen."]) 
        about_window.set_license_type(Gtk.License.CUSTOM) 
        about_window.set_license("Namo is licensed under the GPL v2.") 
        about_window.set_website("https://github.com/hardcoeur/Namo") 
        about_window.set_issue_url("https://github.com/hardcoeur/Namo/issues") 

        about_window.present()


    
    def _load_playlist(self, filepath=None):
        """Loads the playlist from a JSON file. Uses default if filepath is None."""
        path_to_use = filepath if filepath else self._playlist_file_path

        if not os.path.exists(path_to_use):
            if filepath: 
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
                     
                     duration_ns_loaded = item.get('duration_ns')
                     if not isinstance(duration_ns_loaded, int) or duration_ns_loaded < 0:
                         duration_ns_loaded = 0

                     
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
            
            duration_to_save = song.duration if isinstance(song.duration, int) and song.duration >= 0 else 0

            
            song_data_to_save = {
                'uri': song.uri,
                'title': song.title,
                'artist': song.artist,
                'duration_ns': duration_to_save 
            }
            
            if song.album_art_data:
                 raw_bytes = song.album_art_data.get_data()
                 song_data_to_save['album_art_b64'] = base64.b64encode(raw_bytes).decode('ascii')

            
            playlist_data.append(song_data_to_save)

        try:
            
            target_dir = os.path.dirname(path_to_use)
            if target_dir: 
                 os.makedirs(target_dir, exist_ok=True)

            print(f"Saving playlist to: {path_to_use}")
            with open(path_to_use, 'w') as f:
                json.dump(playlist_data, f, indent=2) 

        except Exception as e:
            print(f"Error saving playlist to {path_to_use}: {e}", file=sys.stderr)

    



class NamoApplication(Adw.Application):
    def __init__(self, **kwargs):
        super().__init__(application_id='com.example.NamoMediaPlayer',
                         flags=Gio.ApplicationFlags.FLAGS_NONE,
                        **kwargs)
        GLib.set_application_name("Namo Media Player")
        self.window = None

    def do_activate(self):
        
        if not self.window:
            self.window = NamoWindow(application=self)
        self.window.present()

    def do_startup(self):
        Adw.Application.do_startup(self)

        
        provider = Gtk.CssProvider()
        css_file = os.path.join(os.path.dirname(__file__), "style.css") 
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
        
        if self.window:
             self.window._save_playlist()

        
        if self.window and hasattr(self.window, 'discoverer') and self.window.discoverer:
            print("Stopping discoverer...")
            self.window.discoverer.stop()
        if self.window and hasattr(self.window, 'player') and self.window.player:
             print("Setting player to NULL state...")
             self.window.player.set_state(Gst.State.NULL)

        Adw.Application.do_shutdown(self)


def main():
    
    Gst.init(None)
    
    app = NamoApplication()
    return app.run(sys.argv)

if __name__ == "__main__":
    sys.exit(main())
