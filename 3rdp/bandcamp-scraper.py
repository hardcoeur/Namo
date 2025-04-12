import requests
from bs4 import BeautifulSoup
import os
import re
import json
import sys

def get_artist_album_urls(artist_url):
    response = requests.get(artist_url)
    soup = BeautifulSoup(response.content, 'html.parser')
    album_urls = []

    # Find all album elements
    albums = soup.find_all('li', class_='music-grid-item')
    #print(albums)

    for album in albums:
        album_link = album.find('a', href=True)
        if album_link and album_link['href'].startswith('/album/'):
            full_url = artist_url.rstrip('/') + album_link['href']
            album_urls.append(full_url)
            print(f"Found album URL: {full_url}")

    # Additional albums beyond the first 16 are in the "data-client-items" ol
    # Equivalent to a [data-client-items] CSS selector
    script_tag = soup.find("ol", {"data-client-items": True})
    if script_tag:
        # Get the data-client-items attribute
        data_tralbum = script_tag["data-client-items"]
        # Parse the JSON data
        albums2 = json.loads(data_tralbum)

        for album in albums2:
            #album_url = album.find('a', href=True)['href']
            album_page_url = album.get('page_url')
            if album_page_url and album_page_url.startswith('/album/'):
                 full_url = artist_url.rstrip('/') + album_page_url
                 if full_url not in album_urls: # Avoid duplicates
                     album_urls.append(full_url)
                     print(f"Found album URL (data-client): {full_url}")
    return album_urls

def get_album_track_info(album_url):
    print(f"Getting track info for album: {album_url}")
    response = requests.get(album_url)
    soup = BeautifulSoup(response.content, 'html.parser')
    track_infos = []

    # Extract artist name from album URL if possible (heuristic)
    artist_name_match = re.search(r"https://([^.]+)\.bandcamp\.com", album_url)
    artist_name = artist_name_match.group(1) if artist_name_match else "Unknown Artist"

    # Extract album title from page if possible
    album_title_element = soup.find('h2', class_='trackTitle')
    album_title = album_title_element.text.strip() if album_title_element else "Unknown Album"

    # Find all track elements
    tracks = soup.find_all('tr', class_='track_row_view')

    for track in tracks:
        # Extract track title and URL
        title_element = track.find('span', class_='track-title')
        if title_element:
            title = title_element.text.strip()
            track_link = track.find('a', href=True)
            if track_link and track_link['href'].startswith('/track/'):
                # Construct full track URL relative to the *album* URL's domain
                base_url_match = re.match(r"^(https?://[^/]+)", album_url)
                if base_url_match:
                    base_url = base_url_match.group(1)
                    track_page_url = base_url + track_link['href']
                    print(f"Found track page URL: {track_page_url}")
                    track_info = get_bandcamp_track_info(track_page_url, artist_name, album_title)
                    if track_info:
                        track_infos.append(track_info)
                else:
                    print(f"Could not determine base URL from album URL: {album_url}")
        else:
            print("Could not find title for a track, skipping...")

    print(f"Found {len(track_infos)} tracks for album '{album_title}'")
    return track_infos

def get_bandcamp_track_info(track_page_url, default_artist="Unknown Artist", default_album="Unknown Album"):
    print(f"Getting track info for page: {track_page_url}")
    response = requests.get(track_page_url)

    if response.status_code != 200:
        print(f"Failed to access track page {track_page_url}. Status code: {response.status_code}")
        return None

    tralbum_data_match = re.search(r'data-tralbum="([^"]*)"', response.text)
    if not tralbum_data_match:
        print(f"Failed to find track data on page: {track_page_url}")
        return None

    tralbum_data = json.loads(tralbum_data_match.group(1).replace("&quot;", '"'))

    track_info = tralbum_data['trackinfo'][0]
    track_title = track_info['title']
    stream_url = track_info.get('file', {}).get('mp3-128')

    if not stream_url:
        print(f"No streamable mp3-128 found for track: {track_title}")
        return None

    # Extract artist/album from data if available, otherwise use defaults
    artist = tralbum_data.get('artist', default_artist)
    current_item = tralbum_data.get('current', {})
    album_title = current_item.get('title', default_album)

    # Duration might be available
    duration = track_info.get('duration') # Float seconds

    print(f"Found track info: Title='{track_title}', Artist='{artist}', Album='{album_title}', Duration={duration}, URL='{stream_url}'")

    return {
        "title": track_title,
        "artist": artist,
        "album": album_title,
        "duration": duration, # Store duration in seconds
        "stream_url": stream_url,
        "track_page_url": track_page_url # Keep original page for reference
    }

# Removed main execution block
