#!/usr/bin/env python3

import discord
from discord import app_commands, ui
from discord.ext import commands
import asyncio
from datetime import datetime, timedelta
import logging
import sys
import csv
import io
import copy

import pickle


from config import (
    TOKEN, COMMAND_PREFIX, ORGANIZER_ROLE, CLAN_REP_ROLE, 
    DEFAULT_MAX_SLOTS, DEFAULT_MAX_TEAM_SIZE, EXPANDED_MAX_TEAM_SIZE,
    WAITLIST_CHECK_INTERVAL, ADMIN_IDS
)
from utils import (
    load_data, save_data, format_event_details, format_event_list, 
    has_role, parse_date, logger, send_to_log_channel, discord_handler,
    generate_team_id, export_log_file, clear_log_file, import_log_file
)

# Check if token is available
if not TOKEN:
    logger.critical("No Discord bot token found. Set the DISCORD_BOT_TOKEN environment variable.")
    sys.exit(1)

# Set up Discord intents
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True  # Add this to access member roles

# Initialize bot
class EventBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=COMMAND_PREFIX, intents=intents)
        
    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Slash commands synced")

bot = EventBot()

# Load saved data
event_data, channel_id, user_team_assignments = load_data()
team_requester = {}  # Store users who requested waitlist spots

# Helper functions
def get_event():
    """Get the current event data"""
    # Defensive Programmierung: Stelle sicher, dass event_data existiert und ein Dictionary ist
    if not isinstance(event_data, dict):
        logger.error("event_data ist kein Dictionary")
        return None
    
    # Greife auf den 'event'-Schlüssel in event_data zu
    event = event_data.get('event', {})
    
    # Prüfe, ob ein Event existiert (mindestens eine gültige Eigenschaft)
    if not event:
        return None
    
    if not event.get('name') and not event.get('date'):
        return None
    
    # Prüfe, ob das Event alle erwarteten Schlüssel hat
    required_keys = ['name', 'date', 'time', 'description', 'teams', 'waitlist', 'max_slots', 'slots_used', 'max_team_size']
    for key in required_keys:
        if key not in event:
            logger.warning(f"Event fehlt Schlüssel: {key}")
            # Stelle default-Werte für wichtige Schlüssel bereit
            if key == 'teams':
                event['teams'] = {}
            elif key == 'waitlist':
                event['waitlist'] = []
            elif key in ['max_slots', 'slots_used', 'max_team_size']:
                event[key] = 0
            elif key in ['name', 'date', 'time', 'description']:
                event[key] = ""
    
    return event

def get_user_team(user_id):
    """Get the team name for a user"""
    return user_team_assignments.get(str(user_id))

def get_team_total_size(event, team_name):
    """
    Berechnet die Gesamtgröße eines Teams (Event + Warteliste)
    
    Parameters:
    - event: Eventdaten
    - team_name: Name des Teams (wird als lowercase behandelt)
    
    Returns:
    - Tupel (event_size, waitlist_size, total_size, registered_name, waitlist_entries)
      - event_size: Größe im Event
      - waitlist_size: Gesamtgröße auf der Warteliste
      - total_size: Gesamtgröße (Event + Warteliste)
      - registered_name: Der tatsächliche Name im Event (oder None)
      - waitlist_entries: Liste mit Tupeln (index, team_name, size, team_id) aller Wartelisteneinträge für dieses Team
    """
    team_name = team_name.strip().lower()  # Normalisiere Teamnamen
    
    # Größe und Name im Event (case-insensitive Lookup)
    event_size = 0
    registered_name = None
    team_id = None
    
    # Prüfe, ob das Team-Dictionary jetzt das erweiterte Format mit IDs verwendet
    using_team_ids = False
    if event["teams"] and isinstance(next(iter(event["teams"].values())), dict):
        using_team_ids = True
    
    if using_team_ids:
        # Neues Format mit Team-IDs
        for name, data in event["teams"].items():
            if name.lower() == team_name:
                event_size = data.get("size", 0)
                registered_name = name
                team_id = data.get("id")
                break
    else:
        # Altes Format (abwärtskompatibel)
        for name, size in event["teams"].items():
            if name.lower() == team_name:
                event_size = size
                registered_name = name
                break
    
    # Suche alle Einträge des Teams auf der Warteliste
    waitlist_entries = []
    waitlist_size = 0
    
    # Prüfe, ob die Warteliste das erweiterte Format mit IDs verwendet
    using_waitlist_ids = False
    if event["waitlist"] and len(event["waitlist"][0]) > 2:
        using_waitlist_ids = True
    
    if using_waitlist_ids:
        # Neues Format mit Team-IDs
        for i, entry in enumerate(event["waitlist"]):
            if len(entry) >= 3:  # Format: (team_name, size, team_id)
                wl_team, wl_size, wl_team_id = entry[0], entry[1], entry[2]
                if wl_team.lower() == team_name:
                    waitlist_entries.append((i, wl_team, wl_size, wl_team_id))
                    waitlist_size += wl_size
    else:
        # Altes Format (abwärtskompatibel)
        for i, (wl_team, wl_size) in enumerate(event["waitlist"]):
            if wl_team.lower() == team_name:
                waitlist_entries.append((i, wl_team, wl_size, None))
                waitlist_size += wl_size
    
    # Gesamtgröße
    total_size = event_size + waitlist_size
    
    return (event_size, waitlist_size, total_size, registered_name, waitlist_entries)

# ############################# #
# NEUE HILFSFUNKTIONEN ######### #
# ############################# #

async def validate_command_context(interaction, required_role=None, check_event=True, team_required=False):
    """
    Validiert den Kontext eines Befehls: Event, Rolle, Team-Zugehörigkeit
    
    Parameters:
    - interaction: Discord-Interaktion
    - required_role: Erforderliche Rolle (z.B. ORGANIZER_ROLE oder CLAN_REP_ROLE)
    - check_event: Ob geprüft werden soll, ob ein Event existiert
    - team_required: Ob geprüft werden soll, ob der Benutzer einem Team zugewiesen ist
    
    Returns:
    - Tupel (event, team_name) oder (None, None) bei Fehler
    """
    # Prüfen, ob ein Event existiert
    if check_event:
        event = get_event()
        if not event:
            await interaction.response.send_message("Es gibt derzeit kein aktives Event.", ephemeral=True)
            return None, None
    else:
        event = None

    # Rollenprüfung
    if required_role and not has_role(interaction.user, required_role):
        await interaction.response.send_message(
            f"Nur Mitglieder mit der Rolle '{required_role}' können diese Aktion ausführen.",
            ephemeral=True
        )
        return None, None

    # Team-Zugehörigkeit prüfen
    user_id = str(interaction.user.id)
    team_name = user_team_assignments.get(user_id)
    
    if team_required and not team_name:
        await interaction.response.send_message(
            "Du bist keinem Team zugewiesen.",
            ephemeral=True
        )
        return None, None
        
    return event, team_name

async def validate_team_size(interaction, team_size, max_team_size, allow_zero=True):
    """
    Validiert die Teamgröße gegen die maximale Teamgröße
    
    Parameters:
    - interaction: Discord-Interaktion
    - team_size: Die zu prüfende Teamgröße
    - max_team_size: Die maximale erlaubte Teamgröße
    - allow_zero: Ob 0 als gültige Größe erlaubt ist (für Abmeldungen)
    
    Returns:
    - True wenn gültig, False sonst
    """
    min_size = 0 if allow_zero else 1
    
    if team_size < min_size or team_size > max_team_size:
        await interaction.response.send_message(
            f"Die Teamgröße muss zwischen {min_size} und {max_team_size} liegen.",
            ephemeral=True
        )
        return False
    
    return True

async def send_feedback(interaction, message, ephemeral=True, embed=None, view=None):
    """
    Sendet standardisiertes Feedback an den Benutzer
    
    Parameters:
    - interaction: Discord-Interaktion
    - message: Die zu sendende Nachricht
    - ephemeral: Ob die Nachricht nur für den Benutzer sichtbar sein soll
    - embed: Optional - Ein Discord-Embed zur Anzeige
    - view: Optional - Eine View mit Buttons/anderen UI-Elementen
    
    Returns:
    - True bei erfolgreicher Zustellung
    """
    try:
        # Prüfe ob die Interaktion bereits beantwortet wurde
        response_already_done = False
        try:
            # Verwende is_responded, wenn vorhanden (neuere discord.py-Versionen)
            if hasattr(interaction, 'response') and hasattr(interaction.response, 'is_done'):
                response_already_done = interaction.response.is_done()
            # Fallback für ältere discord.py-Versionen
            elif hasattr(interaction, 'response') and hasattr(interaction.response, 'is_finished'):
                response_already_done = interaction.response.is_finished()
        except Exception:
            # Im Zweifel versuchen wir erst response und dann followup
            pass
        
        # Je nach Zustand der Interaktion den richtigen Sendemechanismus verwenden
        if response_already_done:
            # Die Interaktion wurde bereits beantwortet, also followup verwenden
            if view is None:
                if embed:
                    await interaction.followup.send(message, embed=embed, ephemeral=ephemeral)
                else:
                    await interaction.followup.send(message, ephemeral=ephemeral)
            else:
                if embed:
                    await interaction.followup.send(message, embed=embed, ephemeral=ephemeral, view=view)
                else:
                    await interaction.followup.send(message, ephemeral=ephemeral, view=view)
        else:
            # Die Interaktion wurde noch nicht beantwortet, also response verwenden
            if view is None:
                if embed:
                    await interaction.response.send_message(message, embed=embed, ephemeral=ephemeral)
                else:
                    await interaction.response.send_message(message, ephemeral=ephemeral)
            else:
                if embed:
                    await interaction.response.send_message(message, embed=embed, ephemeral=ephemeral, view=view)
                else:
                    await interaction.response.send_message(message, ephemeral=ephemeral, view=view)
        return True
    except Exception as e:
        logger.error(f"Fehler beim Senden von Feedback: {e}")
        try:
            # Letzter Versuch mit followup, falls alles andere fehlschlägt
            # Prüfe ob view None ist (discord.py erwartet für view ein View-Objekt, nicht None)
            if view is None:
                if embed:
                    await interaction.followup.send(message, embed=embed, ephemeral=ephemeral)
                else:
                    await interaction.followup.send(message, ephemeral=ephemeral)
            else:
                if embed:
                    await interaction.followup.send(message, embed=embed, ephemeral=ephemeral, view=view)
                else:
                    await interaction.followup.send(message, ephemeral=ephemeral, view=view)
            return True
        except Exception as e2:
            logger.error(f"Auch zweiter Versuch fehlgeschlagen: {e2}")
            return False

async def handle_team_unregistration(interaction, team_name, is_admin=False):
    """
    Verarbeitet die Abmeldung eines Teams
    
    Parameters:
    - interaction: Discord-Interaktion
    - team_name: Name des Teams
    - is_admin: Ob die Aktion von einem Admin durchgeführt wird
    
    Returns:
    - True bei erfolgreicher Abmeldung
    """
    event = get_event()
    if not event:
        return False
        
    team_name = team_name.strip().lower()
    
    # Prüfe, ob das Team angemeldet ist oder auf der Warteliste steht
    team_registered = False
    team_on_waitlist = False
    waitlist_indices = []
    
    # Verwende Hilfsfunktion zur Formaterkennung
    from utils import is_using_team_ids, is_using_waitlist_ids
    using_team_ids = is_using_team_ids(event)
    using_waitlist_ids = is_using_waitlist_ids(event)
    
    if using_team_ids:
        # Neues Format mit Team-IDs
        for name in list(event["teams"].keys()):
            if name.lower() == team_name:
                team_registered = True
                break
    else:
        # Altes Format
        for name in list(event["teams"].keys()):
            if name.lower() == team_name:
                team_registered = True
                break
    
    # Suche alle Einträge des Teams auf der Warteliste
    if using_waitlist_ids:
        for i, entry in enumerate(event["waitlist"]):
            if len(entry) >= 3:  # Format: (team_name, size, team_id)
                if entry[0].lower() == team_name:
                    team_on_waitlist = True
                    waitlist_indices.append(i)
    else:
        for i, (wl_team, _) in enumerate(event["waitlist"]):
            if wl_team.lower() == team_name:
                team_on_waitlist = True
                waitlist_indices.append(i)
    
    if not team_registered and not team_on_waitlist:
        await send_feedback(
            interaction,
            f"Team {team_name} ist weder angemeldet noch auf der Warteliste.",
            ephemeral=True
        )
        return False
    
    # Bestätigungsdialog anzeigen
    embed = discord.Embed(
        title="⚠️ Team wirklich abmelden?",
        description=f"Bist du sicher, dass du {'das' if is_admin else 'dein'} Team **{team_name}** abmelden möchtest?\n\n"
                   f"Diese Aktion kann nicht rückgängig gemacht werden!",
        color=discord.Color.red()
    )
    
    # Erstelle die Bestätigungsansicht
    view = TeamUnregisterConfirmationView(team_name, is_admin=is_admin)
    await send_feedback(interaction, "", ephemeral=True, embed=embed, view=view)
    
    # Log für Abmeldebestätigungsdialog
    status = "registriert" if team_registered else "auf der Warteliste"
    action_by = "Admin " if is_admin else ""
    await send_to_log_channel(
        f"🔄 Abmeldungsprozess gestartet: {action_by}{interaction.user.name} ({interaction.user.id}) will Team '{team_name}' abmelden (Status: {status})",
        level="INFO",
        guild=interaction.guild
    )
    
    return True

async def handle_team_size_change(interaction, team_name, old_size, new_size, is_admin=False):
    """
    Verarbeitet die Änderung der Teamgröße (Erhöhung oder Verringerung)
    
    Parameters:
    - interaction: Discord-Interaktion
    - team_name: Name des Teams
    - old_size: Aktuelle Teamgröße
    - new_size: Neue Teamgröße
    - is_admin: Ob die Aktion von einem Admin durchgeführt wird
    
    Returns:
    - Eine Statusnachricht als String
    """
    event = get_event()
    if not event:
        logger.warning(f"Team-Größenänderung für '{team_name}' fehlgeschlagen: Kein aktives Event")
        return "Es gibt derzeit kein aktives Event."
        
    user_id = str(interaction.user.id)
    size_difference = new_size - old_size
    
    # Loggen der Anfrage zur Team-Größenänderung
    action_by = "Admin " if is_admin else ""
    logger.info(f"Team-Größenänderung angefordert: {action_by}{interaction.user.name} ({interaction.user.id}) will Team '{team_name}' von {old_size} auf {new_size} ändern (Diff: {size_difference})")
    
    # Keine Änderung
    if size_difference == 0:
        logger.debug(f"Team-Größenänderung für '{team_name}' übersprungen: Keine Änderung (Größe bleibt {new_size})")
        return f"Team {team_name} ist bereits mit {new_size} Personen angemeldet."
    
    # Abmeldung (size == 0)
    if new_size == 0:
        logger.info(f"Team-Abmeldung erkannt für '{team_name}' (Größe: {old_size})")
        await handle_team_unregistration(interaction, team_name, is_admin)
        return None  # Rückgabe erfolgt in handle_team_unregistration
    
    # Teamgröße erhöhen
    if size_difference > 0:
        # Check if enough slots are available
        if event["slots_used"] + size_difference > event["max_slots"]:
            available_slots = event["max_slots"] - event["slots_used"]
            if available_slots > 0:
                # Teilweise anmelden und Rest auf Warteliste
                waitlist_size = size_difference - available_slots
                
                # Aktualisiere die angemeldete Teamgröße
                event["slots_used"] += available_slots
                
                # Verwende Hilfsfunktion zur Formaterkennung
                from utils import is_using_team_ids
                using_team_ids = is_using_team_ids(event)
                
                if using_team_ids:
                    # Neues Format mit Team-IDs
                    for name, data in event["teams"].items():
                        if name.lower() == team_name.lower():
                            event["teams"][name]["size"] = data.get("size", 0) + available_slots
                            break
                else:
                    # Altes Format
                    for name in event["teams"]:
                        if name.lower() == team_name.lower():
                            event["teams"][name] = old_size + available_slots
                            break
                
                # Füge Rest zur Warteliste hinzu
                # Generiere eine Team-ID, falls noch nicht vorhanden
                team_id = None
                for name, data in event["teams"].items():
                    if name.lower() == team_name.lower():
                        if isinstance(data, dict) and "id" in data:
                            team_id = data["id"]
                        break
                
                if team_id is None:
                    from utils import generate_team_id
                    team_id = generate_team_id(team_name)
                
                # Verwende Hilfsfunktion zur Formaterkennung
                from utils import is_using_waitlist_ids
                using_waitlist_ids = is_using_waitlist_ids(event)
                
                if using_waitlist_ids:
                    event["waitlist"].append((team_name, waitlist_size, team_id))
                else:
                    event["waitlist"].append((team_name, waitlist_size))
                
                # Nutzer diesem Team zuweisen
                user_team_assignments[user_id] = team_name
                
                # Speichere für Benachrichtigungen
                team_requester[team_name] = interaction.user
                
                return (f"Team {team_name} wurde teilweise angemeldet. "
                        f"{old_size + available_slots} Spieler sind angemeldet und "
                        f"{waitlist_size} Spieler wurden auf die Warteliste gesetzt (Position {len(event['waitlist'])}).")
            else:
                # Komplett auf Warteliste setzen
                # Generiere eine Team-ID
                from utils import generate_team_id
                team_id = generate_team_id(team_name)
                
                # Verwende Hilfsfunktion zur Formaterkennung
                from utils import is_using_waitlist_ids
                using_waitlist_ids = is_using_waitlist_ids(event)
                
                if using_waitlist_ids:
                    event["waitlist"].append((team_name, new_size, team_id))
                else:
                    event["waitlist"].append((team_name, new_size))
                
                # Nutzer diesem Team zuweisen
                user_team_assignments[user_id] = team_name
                
                # Speichere für Benachrichtigungen
                team_requester[team_name] = interaction.user
                
                return f"Team {team_name} wurde mit {new_size} Personen auf die Warteliste gesetzt (Position {len(event['waitlist'])})."
        else:
            # Genügend Plätze vorhanden, normal anmelden
            event["slots_used"] += size_difference
            
            # Prüfe, ob das Team-Dictionary das erweiterte Format mit IDs verwendet
            using_team_ids = False
            if event["teams"] and isinstance(next(iter(event["teams"].values())), dict):
                using_team_ids = True
            
            if using_team_ids:
                # Neues Format mit Team-IDs
                team_exists = False
                for name in event["teams"]:
                    if name.lower() == team_name.lower():
                        event["teams"][name]["size"] = new_size
                        team_exists = True
                        break
                
                if not team_exists:
                    # Team neu anlegen
                    from utils import generate_team_id
                    team_id = generate_team_id(team_name)
                    event["teams"][team_name] = {"size": new_size, "id": team_id}
            else:
                # Altes Format
                team_exists = False
                for name in event["teams"]:
                    if name.lower() == team_name.lower():
                        event["teams"][name] = new_size
                        team_exists = True
                        break
                
                if not team_exists:
                    # Team neu anlegen
                    event["teams"][team_name] = new_size
            
            # Assign user to this team
            user_team_assignments[user_id] = team_name
            
            # Log für Team-Anmeldung
            action_by = "Admin " if is_admin else ""
            await send_to_log_channel(
                f"👥 Team angemeldet: {action_by}{interaction.user.name} hat Team '{team_name}' mit {new_size} Mitgliedern angemeldet",
                guild=interaction.guild
            )
            
            return f"Team {team_name} wurde mit {new_size} Personen angemeldet."
    else:  # size_difference < 0
        # Reduce team size
        event["slots_used"] += size_difference  # Will be negative
        
        # Prüfe, ob das Team-Dictionary das erweiterte Format mit IDs verwendet
        using_team_ids = False
        if event["teams"] and isinstance(next(iter(event["teams"].values())), dict):
            using_team_ids = True
        
        if using_team_ids:
            # Neues Format mit Team-IDs
            for name in event["teams"]:
                if name.lower() == team_name.lower():
                    event["teams"][name]["size"] = new_size
                    break
        else:
            # Altes Format
            for name in event["teams"]:
                if name.lower() == team_name.lower():
                    event["teams"][name] = new_size
                    break
        
        result_message = f"Teamgröße für {team_name} wurde auf {new_size} aktualisiert."
        
        # Freie Plätze für Warteliste nutzen
        free_slots = -size_difference
        await process_waitlist_after_change(interaction, free_slots)
        
        return result_message

async def update_event_displays(interaction=None, channel=None):
    """
    Aktualisiert alle Event-Anzeigen im Kanal
    
    Parameters:
    - interaction: Optional - Discord-Interaktion (wenn vorhanden)
    - channel: Optional - Discord-Kanal (wenn keine Interaktion vorhanden)
    
    Returns:
    - True bei Erfolg, False bei Fehler
    """
    try:
        if not channel and interaction:
            if channel_id:
                channel = bot.get_channel(channel_id)
            else:
                channel = bot.get_channel(interaction.channel_id)
        
        if channel:
            await send_event_details(channel)
            return True
        return False
    except Exception as e:
        logger.error(f"Fehler beim Aktualisieren der Event-Anzeigen: {e}")
        return False

async def process_waitlist_after_change(interaction, free_slots):
    """
    Verarbeitet die Warteliste, nachdem Slots frei geworden sind.
    
    Parameters:
    - interaction: Discord-Interaktion
    - free_slots: Anzahl der frei gewordenen Slots
    """
    if free_slots <= 0:
        logger.debug(f"Keine freien Slots verfügbar für Wartelisten-Verarbeitung (free_slots={free_slots})")
        return
    
    event = get_event()
    if not event:
        logger.debug("Kein Event gefunden für Wartelisten-Verarbeitung")
        return
        
    if not event.get('waitlist'):
        logger.debug("Keine Warteliste im Event vorhanden")
        return
        
    logger.info(f"Wartelisten-Verarbeitung gestartet: {free_slots} freie Slots, {len(event['waitlist'])} Teams auf Warteliste")
    
    # Solange freie Plätze vorhanden sind und die Warteliste nicht leer ist
    while free_slots > 0 and event["waitlist"]:
        # Nehme den ersten Eintrag von der Warteliste
        # Prüfe, ob die Warteliste das erweiterte Format mit IDs verwendet
        using_waitlist_ids = False
        if event["waitlist"] and len(event["waitlist"][0]) > 2:
            using_waitlist_ids = True
        
        if using_waitlist_ids:
            # Neues Format mit Team-IDs
            entry = event["waitlist"][0]
            wait_team_name, wait_size, wait_team_id = entry
        else:
            # Altes Format
            wait_team_name, wait_size = event["waitlist"][0]
            wait_team_id = None
        
        # Prüfe, ob das gesamte Team Platz hat
        if wait_size <= free_slots:
            # Das ganze Team kann nachrücken
            # Entferne Team von der Warteliste
            event["waitlist"].pop(0)
            
            # Füge Team zum Event hinzu
            event["slots_used"] += wait_size
            free_slots -= wait_size
            
            # Prüfe, ob das Team bereits im Event ist (mit anderer Größe)
            team_in_event = False
            
            # Prüfe, ob das Team-Dictionary das erweiterte Format mit IDs verwendet
            using_team_ids = False
            if event["teams"] and isinstance(next(iter(event["teams"].values())), dict):
                using_team_ids = True
            
            if using_team_ids:
                # Neues Format mit Team-IDs
                for name, data in event["teams"].items():
                    if name.lower() == wait_team_name.lower():
                        # Erhöhe die Größe des bestehenden Teams
                        event["teams"][name]["size"] = data.get("size", 0) + wait_size
                        team_in_event = True
                        break
                
                if not team_in_event:
                    # Füge neues Team hinzu
                    if wait_team_id:
                        event["teams"][wait_team_name] = {"size": wait_size, "id": wait_team_id}
                    else:
                        from utils import generate_team_id
                        team_id = generate_team_id(wait_team_name)
                        event["teams"][wait_team_name] = {"size": wait_size, "id": team_id}
            else:
                # Altes Format
                for name in event["teams"]:
                    if name.lower() == wait_team_name.lower():
                        # Erhöhe die Größe des bestehenden Teams
                        event["teams"][name] += wait_size
                        team_in_event = True
                        break
                
                if not team_in_event:
                    # Füge neues Team hinzu
                    event["teams"][wait_team_name] = wait_size
            
            # Sende Benachrichtigung an den Team-Leiter
            await send_team_dm_notification(
                wait_team_name, 
                f"🎉 Dein Team **{wait_team_name}** ist von der Warteliste ins Event nachgerückt!"
            )
            
            # Team-Channel mit Benachrichtigung
            await send_to_log_channel(
                f"⬆️ Team nachgerückt: '{wait_team_name}' mit {wait_size} Mitgliedern ist von der Warteliste ins Event nachgerückt",
                guild=interaction.guild
            )
        else:
            # Das Team kann nur teilweise nachrücken
            # Aktualisiere die Größe auf der Warteliste
            if using_waitlist_ids:
                event["waitlist"][0] = (wait_team_name, wait_size - free_slots, wait_team_id)
            else:
                event["waitlist"][0] = (wait_team_name, wait_size - free_slots)
            
            # Prüfe, ob das Team bereits im Event ist
            team_in_event = False
            
            # Prüfe, ob das Team-Dictionary das erweiterte Format mit IDs verwendet
            using_team_ids = False
            if event["teams"] and isinstance(next(iter(event["teams"].values())), dict):
                using_team_ids = True
            
            if using_team_ids:
                # Neues Format mit Team-IDs
                for name, data in event["teams"].items():
                    if name.lower() == wait_team_name.lower():
                        # Erhöhe die Größe des bestehenden Teams
                        event["teams"][name]["size"] = data.get("size", 0) + free_slots
                        team_in_event = True
                        break
                
                if not team_in_event:
                    # Füge neues Team hinzu
                    if wait_team_id:
                        event["teams"][wait_team_name] = {"size": free_slots, "id": wait_team_id}
                    else:
                        from utils import generate_team_id
                        team_id = generate_team_id(wait_team_name)
                        event["teams"][wait_team_name] = {"size": free_slots, "id": team_id}
            else:
                # Altes Format
                for name in event["teams"]:
                    if name.lower() == wait_team_name.lower():
                        # Erhöhe die Größe des bestehenden Teams
                        event["teams"][name] += free_slots
                        team_in_event = True
                        break
                
                if not team_in_event:
                    # Füge neues Team hinzu
                    event["teams"][wait_team_name] = free_slots
            
            # Aktualisiere die belegten Slots
            event["slots_used"] += free_slots
            
            # Sende Benachrichtigung an den Team-Leiter
            await send_team_dm_notification(
                wait_team_name, 
                f"🎉 Teile deines Teams **{wait_team_name}** sind von der Warteliste ins Event nachgerückt! "
                f"{free_slots} Mitglieder sind jetzt angemeldet, {wait_size - free_slots} bleiben auf der Warteliste."
            )
            
            # Team-Channel mit Benachrichtigung
            await send_to_log_channel(
                f"⬆️ Team teilweise nachgerückt: '{wait_team_name}' mit {free_slots} Mitgliedern ist teilweise von der Warteliste ins Event nachgerückt "
                f"({wait_size - free_slots} bleiben auf der Warteliste)",
                guild=interaction.guild
            )
            
            # Alle freien Plätze sind belegt
            free_slots = 0
    
    # Aktualisiere die Event-Anzeige
    await update_event_displays(interaction=interaction)

async def send_team_dm_notification(team_name, message):
    """
    Sendet eine DM-Benachrichtigung an den Teamleiter.
    
    Parameters:
    - team_name: Name des Teams
    - message: Nachricht, die gesendet werden soll
    """
    try:
        # Suche den Team-Requester
        if team_name in team_requester:
            user = team_requester[team_name]
            await user.send(message)
    except Exception as e:
        logger.error(f"Fehler beim Senden einer DM an den Teamleiter: {e}")

async def update_team_size(interaction, team_name, new_size, is_admin=False, reason=None):
    """
    Aktualisiert die Größe eines Teams und verwaltet die Warteliste entsprechend.
    Behandelt Teams als Einheit, unabhängig von Event/Warteliste-Platzierung.
    
    Parameters:
    - interaction: Discord-Interaktion
    - team_name: Name des Teams
    - new_size: Neue Teamgröße
    - is_admin: Ob die Änderung von einem Admin durchgeführt wird
    - reason: Optionaler Grund für die Änderung (nur für Admins)
    
    Returns:
    - True bei Erfolg, False bei Fehler
    """
    event = get_event()
    if not event:
        await send_feedback(interaction, "Es gibt derzeit kein aktives Event.", ephemeral=True)
        return False
    
    # Teamgröße validieren
    if not await validate_team_size(interaction, new_size, event["max_team_size"]):
        return False
    
    # Team-Details abrufen
    team_name = team_name.strip()
    event_size, waitlist_size, total_size, registered_name, waitlist_entries = get_team_total_size(event, team_name)
    
    # Team existiert nicht und soll abgemeldet werden
    if total_size == 0 and new_size == 0:
        await send_feedback(interaction, f"Team {team_name} ist nicht angemeldet.", ephemeral=True)
        return False
    
    # Keine Änderung
    if total_size == new_size:
        await send_feedback(interaction, f"Team {team_name} ist bereits mit {new_size} Spielern angemeldet.", ephemeral=True)
        return False
    
    # Abmeldung
    if new_size == 0:
        return await handle_team_unregistration(interaction, team_name, is_admin)
    
    # Hier kommt die eigentliche Logik für die Größenänderung
    if total_size < new_size:
        # Teamgröße erhöhen
        size_increase = new_size - total_size
        
        # Freie Plätze berechnen
        free_slots = event["max_slots"] - event["slots_used"]
        
        # Wenn genug Platz ist, alle ins Event
        if free_slots >= size_increase:
            # Komplett ins Event (entweder neues Team oder Vergrößerung)
            return await handle_team_size_change(interaction, team_name, total_size, new_size, is_admin)
        elif free_slots > 0:
            # Teilweise ins Event, Rest auf Warteliste
            return await handle_team_size_change(interaction, team_name, total_size, new_size, is_admin)
        else:
            # Komplett auf Warteliste
            return await handle_team_size_change(interaction, team_name, total_size, new_size, is_admin)
    else:
        # Teamgröße verringern
        return await handle_team_size_change(interaction, team_name, total_size, new_size, is_admin)
    
    # Wir sollten nie hierher kommen
    await send_feedback(interaction, "Es ist ein unerwarteter Fehler aufgetreten.", ephemeral=True)
    return False
    
async def admin_add_team(interaction, team_name, size, discord_user_id=None, discord_username=None, force_waitlist=False):
    """
    Funktion für Admins, um ein Team hinzuzufügen
    
    Parameters:
    - interaction: Discord-Interaktion
    - team_name: Name des Teams
    - size: Größe des Teams
    - discord_user_id: Optional - Discord-ID des Nutzers, der dem Team zugewiesen wird
    - discord_username: Optional - Username des Nutzers
    - force_waitlist: Ob das Team direkt auf die Warteliste gesetzt werden soll
    
    Returns:
    - True bei Erfolg, False bei Fehler
    """
    event = get_event()
    if not event:
        await send_feedback(interaction, "Es gibt derzeit kein aktives Event.", ephemeral=True)
        return False
    
    # Teamgröße validieren
    if not await validate_team_size(interaction, size, event["max_team_size"], allow_zero=False):
        return False
    
    team_name = team_name.strip()
    
    # Prüfe, ob das Team bereits existiert
    event_size, waitlist_size, total_size, registered_name, waitlist_entries = get_team_total_size(event, team_name)
    
    if total_size > 0:
        await send_feedback(
            interaction, 
            f"Team {team_name} ist bereits registriert (Event: {event_size}, Warteliste: {waitlist_size}).",
            ephemeral=True
        )
        return False
    
    # Wenn ein Discord-Nutzer angegeben wurde, prüfe, ob dieser bereits einem Team zugewiesen ist
    if discord_user_id:
        user_id = str(discord_user_id)
        if user_id in user_team_assignments:
            assigned_team = user_team_assignments[user_id]
            await send_feedback(
                interaction,
                f"Der Nutzer ist bereits dem Team '{assigned_team}' zugewiesen.",
                ephemeral=True
            )
            return False
    
    # Prüfe, ob genug Platz im Event ist (es sei denn, force_waitlist ist True)
    available_slots = event["max_slots"] - event["slots_used"]
    
    if not force_waitlist and available_slots >= size:
        # Genug Platz im Event - füge Team direkt hinzu
        # Prüfe, ob das Team-Dictionary das erweiterte Format mit IDs verwendet
        using_team_ids = False
        if event["teams"] and isinstance(next(iter(event["teams"].values())), dict):
            using_team_ids = True
        
        if using_team_ids:
            # Neues Format mit Team-IDs
            from utils import generate_team_id
            team_id = generate_team_id(team_name)
            event["teams"][team_name] = {"size": size, "id": team_id}
        else:
            # Altes Format
            event["teams"][team_name] = size
        
        # Aktualisiere die belegten Slots
        event["slots_used"] += size
        
        # Wenn ein Discord-Nutzer angegeben wurde, weise ihn diesem Team zu
        if discord_user_id:
            user_id = str(discord_user_id)
            user_team_assignments[user_id] = team_name
        
        # Log eintragen
        admin_action = f"Admin {interaction.user.name} hat"
        user_info = ""
        if discord_username:
            user_info = f" für Nutzer {discord_username}"
        
        await send_to_log_channel(
            f"👥 Team vom Admin hinzugefügt: {admin_action} Team '{team_name}' mit {size} Spielern{user_info} zum Event hinzugefügt",
            level="INFO",
            guild=interaction.guild
        )
        
        await send_feedback(
            interaction,
            f"Team {team_name} wurde mit {size} Spielern zum Event hinzugefügt.",
            ephemeral=True
        )
    else:
        # Nicht genug Platz oder force_waitlist ist True - füge Team zur Warteliste hinzu
        # Generiere eine Team-ID
        from utils import generate_team_id
        team_id = generate_team_id(team_name)
        
        # Prüfe, ob die Warteliste das erweiterte Format mit IDs verwendet
        using_waitlist_ids = False
        if event["waitlist"] and len(event["waitlist"][0]) > 2:
            using_waitlist_ids = True
        
        if using_waitlist_ids:
            event["waitlist"].append((team_name, size, team_id))
        else:
            event["waitlist"].append((team_name, size))
        
        # Wenn ein Discord-Nutzer angegeben wurde, weise ihn diesem Team zu
        if discord_user_id:
            user_id = str(discord_user_id)
            user_team_assignments[user_id] = team_name
        
        # Log eintragen
        admin_action = f"Admin {interaction.user.name} hat"
        user_info = ""
        if discord_username:
            user_info = f" für Nutzer {discord_username}"
        
        reason = "erzwungen" if force_waitlist else "wegen Platzmangel"
        
        await send_to_log_channel(
            f"👥 Team vom Admin auf Warteliste: {admin_action} Team '{team_name}' mit {size} Spielern{user_info} zur Warteliste hinzugefügt ({reason})",
            level="INFO",
            guild=interaction.guild
        )
        
        await send_feedback(
            interaction,
            f"Team {team_name} wurde mit {size} Spielern auf die Warteliste gesetzt (Position {len(event['waitlist'])}).",
            ephemeral=True
        )
    
    # Aktualisiere die Event-Anzeige
    await update_event_displays(interaction=interaction)
    
    return True

# UI-Komponenten
class TeamRegistrationModal(ui.Modal):
    """Modal für die Team-Anmeldung"""
    def __init__(self, user):
        super().__init__(title="Team anmelden")
        self.user = user
        
        # Felder für Team-Name und -Größe
        self.team_name = ui.TextInput(
            label="Team-Name",
            placeholder="Gib den Namen deines Teams ein",
            required=True,
            min_length=2,
            max_length=30
        )
        self.add_item(self.team_name)
        
        self.team_size = ui.TextInput(
            label="Team-Größe",
            placeholder="Anzahl der Spieler ",
            required=True,
            min_length=1,
            max_length=2
        )
        self.add_item(self.team_size)
    
    async def on_submit(self, interaction: discord.Interaction):
        # Definiere user_id aus der Interaktion
        user_id = str(interaction.user.id)
        
        # Verarbeite die Teamregistrierungslogik
        team_name = self.team_name.value.strip()  # Behalte Originalschreibweise
        
        try:
            size = int(self.team_size.value)
        except ValueError:
            await interaction.response.send_message(
                "Bitte gib eine gültige Zahl für die Team-Größe ein.",
                ephemeral=True
            )
            return
        
        # Hole das aktive Event
        event = get_event()
        if not event:
            await interaction.response.send_message(
                "Es gibt derzeit kein aktives Event.",
                ephemeral=True
            )
            return
        
        # Prüfe, ob der Nutzer bereits einem anderen Team zugewiesen ist (case-insensitive)
        if user_id in user_team_assignments and user_team_assignments[user_id].lower() != team_name.lower():
            assigned_team_name = user_team_assignments[user_id]
            await interaction.response.send_message(
                f"Du bist bereits dem Team '{assigned_team_name}' zugewiesen. Du kannst nur für ein Team anmelden.",
                ephemeral=True
            )
            return
        
        # Speichere den Benutzer für Benachrichtigungen
        team_requester[team_name] = interaction.user
        
        # Verwende die zentrale update_team_size Funktion für die eigentliche Logik
        success = await update_team_size(interaction, team_name, size)
        
        if success:
            # Die Daten werden bereits von update_team_size gespeichert
            # Die Event-Anzeige wird bereits von update_team_size aktualisiert
            pass

# Die TeamWaitlistModal-Klasse wurde entfernt, da die Warteliste jetzt automatisch verwaltet wird

class TeamEditModal(ui.Modal):
    """Modal zum Bearbeiten der Teamgröße"""
    def __init__(self, team_name, current_size, max_size, is_admin=False):
        super().__init__(title=f"Team {team_name} bearbeiten")
        self.team_name = team_name.strip()  # Behalte Originalschreibweise für Anzeige
        self.current_size = current_size
        self.is_admin = is_admin
        
        # Feld für die neue Teamgröße
        self.team_size = ui.TextInput(
            label="Neue Teamgröße",
            placeholder=f"Aktuelle Größe: {current_size} (Max: {max_size})",
            required=True,
            min_length=1,
            max_length=2,
            default=str(current_size)
        )
        self.add_item(self.team_size)
        
        # Für Admins: Optionales Feld für Kommentar/Grund
        if is_admin:
            self.reason = ui.TextInput(
                label="Grund für die Änderung (optional)",
                placeholder="z.B. 'Spieler hat abgesagt'",
                required=False,
                max_length=100
            )
            self.add_item(self.reason)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_size = int(self.team_size.value)
        except ValueError:
            await interaction.response.send_message(
                "Bitte gib eine gültige Zahl für die Teamgröße ein.",
                ephemeral=True
            )
            return
        
        # Rufe die Funktion auf, die die Teamgröße ändert
        result = await update_team_size(
            interaction, 
            self.team_name, 
            new_size, 
            is_admin=self.is_admin,
            reason=self.reason.value if self.is_admin and hasattr(self, 'reason') else None
        )

class AdminTeamCreateModal(ui.Modal):
    """Modal für Admins zum Hinzufügen eines Teams"""
    def __init__(self):
        super().__init__(title="Team hinzufügen")
        
        # Felder für Team-Name und -Größe
        self.team_name = ui.TextInput(
            label="Team-Name",
            placeholder="Gib den Namen des Teams ein",
            required=True,
            min_length=2,
            max_length=30
        )
        self.add_item(self.team_name)
        
        self.team_size = ui.TextInput(
            label="Team-Größe",
            placeholder="Anzahl der Spieler",
            required=True,
            min_length=1,
            max_length=2
        )
        self.add_item(self.team_size)
        
        self.discord_user = ui.TextInput(
            label="Discord-Nutzer (optional)",
            placeholder="Discord Nutzername oder ID für Teamzuweisung",
            required=False
        )
        self.add_item(self.discord_user)
        
        self.add_to_waitlist = ui.TextInput(
            label="Auf Warteliste?",
            placeholder="Ja/Nein (leer = automatisch)",
            required=False,
            max_length=5
        )
        self.add_item(self.add_to_waitlist)
    
    async def on_submit(self, interaction: discord.Interaction):
        # Überprüfe Berechtigung
        if not has_role(interaction.user, ORGANIZER_ROLE):
            await interaction.response.send_message(
                f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können diese Aktion ausführen.",
                ephemeral=True
            )
            return
        
        team_name = self.team_name.value.strip().lower()
        
        try:
            size = int(self.team_size.value)
        except ValueError:
            await interaction.response.send_message(
                "Bitte gib eine gültige Zahl für die Team-Größe ein.",
                ephemeral=True
            )
            return
        
        # Prüfe Wartelisten-Option
        force_waitlist = False
        if self.add_to_waitlist.value.strip().lower() in ["ja", "yes", "true", "1", "y"]:
            force_waitlist = True
        
        # Discord-User prüfen
        discord_user_input = self.discord_user.value.strip()
        discord_user_id = None
        discord_username = None
        
        if discord_user_input:
            # Versuche, den Nutzer zu finden
            try:
                # Versuche als ID zu interpretieren
                if discord_user_input.isdigit():
                    user = await bot.fetch_user(int(discord_user_input))
                    discord_user_id = str(user.id)
                    discord_username = user.display_name
                else:
                    # Versuche Benutzer anhand des Namens zu finden
                    guild = interaction.guild
                    if guild:
                        found_members = [member for member in guild.members if 
                                         member.name.lower() == discord_user_input.lower() or 
                                         (member.nick and member.nick.lower() == discord_user_input.lower())]
                        
                        if found_members:
                            user = found_members[0]
                            discord_user_id = str(user.id)
                            discord_username = user.display_name
                        else:
                            await interaction.response.send_message(
                                f"Konnte keinen Nutzer mit dem Namen '{discord_user_input}' finden.",
                                ephemeral=True
                            )
                            return
            except Exception as e:
                logger.error(f"Fehler beim Suchen des Discord-Nutzers: {e}")
                await interaction.response.send_message(
                    f"Fehler beim Suchen des Discord-Nutzers: {e}",
                    ephemeral=True
                )
                return
        
        # Füge das Team hinzu
        result = await admin_add_team(
            interaction, 
            team_name, 
            size, 
            discord_user_id, 
            discord_username,
            force_waitlist
        )

class BaseView(ui.View):
    """Basis-View für alle Discord-UI-Komponenten mit erweitertem Timeout-Handling und Fehlerbehandlung"""
    def __init__(self, timeout=900, title="Interaktion"):
        super().__init__(timeout=timeout)
        self.has_responded = False  # Tracking-Variable für Interaktionen
        self.message = None
        self.timeout_title = title
    
    async def on_timeout(self):
        """Wird aufgerufen, wenn der Timeout abläuft"""
        try:
            # Buttons deaktivieren
            for child in self.children:
                child.disabled = True
            
            # Ursprüngliche Nachricht editieren, falls möglich
            if hasattr(self, 'message') and self.message:
                try:
                    await self.message.edit(
                        content=f"⏱️ **Zeitüberschreitung** - Die {self.timeout_title}-Anfrage ist abgelaufen. Bitte starte den Vorgang neu.",
                        view=self
                    )
                except discord.errors.NotFound:
                    # Nachricht existiert nicht mehr, ignorieren
                    logger.debug(f"Timeout-Nachricht konnte nicht editiert werden: Nachricht nicht gefunden")
                except discord.errors.Forbidden:
                    # Keine Berechtigung, ignorieren
                    logger.debug(f"Timeout-Nachricht konnte nicht editiert werden: Keine Berechtigung")
        except Exception as e:
            # Allgemeine Fehlerbehandlung als Fallback
            logger.warning(f"Fehler beim Timeout-Handling: {e}")
    
    def store_message(self, interaction):
        """Speichert die Nachricht für spätere Aktualisierungen"""
        self.message = interaction.message
        return self.message
    
    def check_response(self, interaction, store_msg=True):
        """Überprüft, ob die Interaktion bereits beantwortet wurde
        
        Parameters:
        - interaction: Discord-Interaktion
        - store_msg: Ob die Nachrichten-Referenz gespeichert werden soll
        
        Returns:
        - True, wenn die Interaktion bereits beantwortet wurde
        - False, wenn die Interaktion noch nicht beantwortet wurde
        """
        # Speichere die ursprüngliche Nachricht für spätere Aktualisierungen
        if store_msg:
            self.store_message(interaction)
        
        if self.has_responded:
            return True
            
        self.has_responded = True
        return False
    
    async def handle_already_responded(self, interaction, message="Diese Aktion wird bereits verarbeitet..."):
        """Einheitliche Behandlung für bereits beantwortete Interaktionen
        
        Parameters:
        - interaction: Discord-Interaktion
        - message: Optionale Nachricht, die gesendet werden soll
        """
        try:
            await interaction.followup.send(message, ephemeral=True)
        except Exception:
            pass  # Ignoriere Fehler hier, um andere Funktionalität nicht zu beeinträchtigen


class BaseConfirmationView(BaseView):
    """Basis-View für alle Bestätigungsdialoge mit Timeout-Handling und Response-Tracking"""
    def __init__(self, timeout=3600, title="Bestätigung"):
        super().__init__(timeout=timeout, title=title)


class AdminTeamSelector(BaseView):
    """Auswahl eines Teams für die Bearbeitung durch Admins"""
    def __init__(self, for_removal=False):
        super().__init__(timeout=3600, title="Admin-Teamauswahl")
        self.selected_team = None
        self.for_removal = for_removal  # Flag, ob die Auswahl für die Abmeldung ist
        
        # Dropdown für die Teamauswahl
        options = self.get_team_options()
        
        # Prüfe, ob Optionen vorhanden sind
        if not options:
            # Füge eine Dummy-Option hinzu, wenn keine Teams vorhanden sind
            options = [
                discord.SelectOption(
                    label="Keine Teams verfügbar",
                    value="no_teams",
                    description="Es sind keine Teams zum Bearbeiten verfügbar"
                )
            ]
        
        self.teams_select = ui.Select(
            placeholder="Wähle ein Team aus",
            options=options,
            custom_id="team_selector"
        )
        self.teams_select.callback = self.team_selected
        self.add_item(self.teams_select)
    
    def get_team_options(self):
        """Erstellt die Liste der Teams für das Dropdown"""
        event = get_event()
        if not event:
            return []
        
        team_options = []
        
        # Liste der angemeldeten Teams
        for team_name, size in event["teams"].items():
            team_options.append(
                discord.SelectOption(
                    label=f"{team_name} ({size} Personen)",
                    value=team_name,
                    description=f"Angemeldet mit {size} Personen"
                )
            )
        
        # Liste der Teams auf der Warteliste
        for i, (team_name, size) in enumerate(event["waitlist"]):
            team_options.append(
                discord.SelectOption(
                    label=f"{team_name} ({size} Personen)",
                    value=f"waitlist_{team_name}",
                    description=f"Auf Warteliste (Position {i+1})",
                    emoji="⏳"
                )
            )
        
        return team_options
    
    async def team_selected(self, interaction: discord.Interaction):
        """Callback für die Teamauswahl"""
        selected_value = self.teams_select.values[0]
        
        if selected_value == "no_teams":
            await interaction.response.send_message(
                "Es sind keine Teams zum Bearbeiten verfügbar.",
                ephemeral=True
            )
            return
        
        # Prüfe, ob es sich um ein Team auf der Warteliste handelt
        if selected_value.startswith("waitlist_"):
            team_name = selected_value[9:]  # Entferne "waitlist_" Präfix
            is_waitlist = True
        else:
            team_name = selected_value
            is_waitlist = False
        
        # Hole Informationen zum ausgewählten Team
        event = get_event()
        if not event:
            await interaction.response.send_message(
                "Es gibt derzeit kein aktives Event.",
                ephemeral=True
            )
            return
        
        # Wenn die Auswahl für das Abmelden des Teams ist
        if self.for_removal:
            # Bestätigungsdialog anzeigen
            embed = discord.Embed(
                title="⚠️ Team wirklich abmelden?",
                description=f"Bist du sicher, dass du das Team **{team_name}** abmelden möchtest?\n\n"
                           f"Diese Aktion kann nicht rückgängig gemacht werden!",
                color=discord.Color.red()
            )
            
            # Erstelle die Bestätigungsansicht
            view = TeamUnregisterConfirmationView(team_name, is_admin=True)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return
        
        # Ansonsten normale Bearbeitung (für Teamgröße ändern)
        if is_waitlist:
            # Suche Team in der Warteliste
            team_found = False
            team_size = 0
            position = 0
            for i, (wl_team, wl_size) in enumerate(event["waitlist"]):
                if wl_team == team_name:
                    team_found = True
                    team_size = wl_size
                    position = i + 1
                    break
            
            if not team_found:
                await interaction.response.send_message(
                    f"Team {team_name} wurde nicht auf der Warteliste gefunden.",
                    ephemeral=True
                )
                return
            
            # Erstelle ein Modal zur Bearbeitung des Teams auf der Warteliste
            modal = TeamEditModal(team_name, team_size, event["max_team_size"], is_admin=True)
            await interaction.response.send_modal(modal)
        else:
            # Suche Team in den angemeldeten Teams
            if team_name not in event["teams"]:
                await interaction.response.send_message(
                    f"Team {team_name} wurde nicht gefunden.",
                    ephemeral=True
                )
                return
            
            team_size = event["teams"][team_name]
            
            # Erstelle ein Modal zur Bearbeitung des angemeldeten Teams
            modal = TeamEditModal(team_name, team_size, event["max_team_size"], is_admin=True)
            await interaction.response.send_modal(modal)

class EventActionView(BaseView):
    """View mit Buttons für Event-Aktionen"""
    def __init__(self, event, user_has_admin=False, user_has_clan_rep=False, has_team=False, team_name=None):
        super().__init__(timeout=3600, title="Event-Aktionen")  # 1 Stunde Timeout
        self.team_name = team_name
        
        # Team anmelden Button (nur für Clan-Rep)
        register_button = ui.Button(
            label="Team anmelden",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"event_register",
            disabled=not user_has_clan_rep or has_team
        )
        register_button.callback = self.register_callback
        self.add_item(register_button)
        
        # Team abmelden Button (nur für Clan-Rep mit Team)
        if has_team and team_name:
            unregister_button = ui.Button(
                label="Team abmelden",
                emoji="❌",
                style=discord.ButtonStyle.danger,
                custom_id=f"event_unregister",
                disabled=not user_has_clan_rep
            )
            unregister_button.callback = self.unregister_callback
            self.add_item(unregister_button)
        
        # Warteliste wird automatisch verwaltet, daher kein Button mehr erforderlich
        
        # Team-Info für alle sichtbar
        team_info_button = ui.Button(
            label="Mein Team", 
            emoji="👥",
            style=discord.ButtonStyle.primary,
            custom_id=f"event_teaminfo"
        )
        team_info_button.callback = self.team_info_callback
        self.add_item(team_info_button)
        
        # Team bearbeiten Button (für Clan-Rep mit Team und Admins)
        if (user_has_clan_rep and has_team) or user_has_admin:
            edit_button = ui.Button(
                label="Team bearbeiten", 
                emoji="✏️",
                style=discord.ButtonStyle.primary,
                custom_id=f"event_edit_team"
            )
            edit_button.callback = self.edit_team_callback
            self.add_item(edit_button)
            
            # Team abmelden Button (für Clan-Rep mit Team)
            if user_has_clan_rep and has_team:
                unregister_button = ui.Button(
                    label="Team abmelden", 
                    emoji="❌",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"event_unregister_team"
                )
                unregister_button.callback = self.unregister_callback
                self.add_item(unregister_button)
        
        # Admin-Aktionen (nur für Admins)
        if user_has_admin:
            admin_button = ui.Button(
                label="Admin", 
                emoji="⚙️",
                style=discord.ButtonStyle.danger,
                custom_id=f"event_admin"
            )
            admin_button.callback = self.admin_callback
            self.add_item(admin_button)
    
    async def register_callback(self, interaction: discord.Interaction):
        """Callback für Team-Registrierung-Button"""
        user_id = str(interaction.user.id)
        
        # Prüfe, ob der Benutzer bereits einem Team zugewiesen ist
        if user_id in user_team_assignments:
            team_name = user_team_assignments[user_id]
            await interaction.response.send_message(
                f"Du bist bereits dem Team '{team_name}' zugewiesen. Du kannst nicht erneut registrieren.",
                ephemeral=True
            )
            # Log für Versuch einer doppelten Registrierung
            await send_to_log_channel(
                f"ℹ️ Registrierungsversuch abgelehnt: Benutzer {interaction.user.name} ({interaction.user.id}) ist bereits Team '{team_name}' zugewiesen",
                level="INFO",
                guild=interaction.guild
            )
            return
        
        # Überprüfe Berechtigung mit der verbesserten has_role-Funktion
        # Die has_role-Funktion berücksichtigt jetzt auch ADMIN_IDs in DM-Kontexten
        if not has_role(interaction.user, CLAN_REP_ROLE):
            await interaction.response.send_message(
                f"Nur Mitglieder mit der Rolle '{CLAN_REP_ROLE}' können Teams anmelden.",
                ephemeral=True
            )
            # Log für unberechtigten Zugriff
            await send_to_log_channel(
                f"🚫 Unberechtigter Zugriffsversuch: {interaction.user.name} ({interaction.user.id}) hat versucht, ein Team zu registrieren ohne die Rolle '{CLAN_REP_ROLE}'",
                level="WARNING",
                guild=interaction.guild
            )
            return
        
        # Öffne ein Modal für die Team-Anmeldung
        modal = TeamRegistrationModal(interaction.user)
        await interaction.response.send_modal(modal)
        
        # Log für Registrierungsversuch
        await send_to_log_channel(
            f"🔄 Registrierungsvorgang gestartet: {interaction.user.name} ({interaction.user.id}) öffnet das Team-Registrierungsformular",
            level="INFO",
            guild=interaction.guild
        )
    
    async def unregister_callback(self, interaction: discord.Interaction):
        """Callback für Team-Abmeldung-Button"""
        user_id = str(interaction.user.id)
        
        # Überprüfe Berechtigung mit der verbesserten has_role-Funktion
        if not has_role(interaction.user, CLAN_REP_ROLE):
            await interaction.response.send_message(
                f"Nur Mitglieder mit der Rolle '{CLAN_REP_ROLE}' können Teams abmelden.",
                ephemeral=True
            )
            # Log für unberechtigten Zugriff
            await send_to_log_channel(
                f"🚫 Unberechtigter Zugriffsversuch: {interaction.user.name} ({interaction.user.id}) hat versucht, ein Team abzumelden ohne die Rolle '{CLAN_REP_ROLE}'",
                level="WARNING",
                guild=interaction.guild
            )
            return
        
        team_name = user_team_assignments.get(user_id)
        
        if not team_name:
            await interaction.response.send_message(
                "Du bist keinem Team zugewiesen.",
                ephemeral=True
            )
            # Log für fehlgeschlagene Abmeldung
            await send_to_log_channel(
                f"ℹ️ Abmeldungsversuch abgelehnt: Benutzer {interaction.user.name} ({interaction.user.id}) ist keinem Team zugewiesen",
                level="INFO",
                guild=interaction.guild
            )
            return
            
        event = get_event()
        if not event:
            await interaction.response.send_message("Es gibt kein aktives Event.", ephemeral=True)
            await send_to_log_channel(
                f"⚠️ Abmeldungsversuch fehlgeschlagen: Kein aktives Event vorhanden (Benutzer: {interaction.user.name})",
                level="WARNING",
                guild=interaction.guild
            )
            return
            
        # Prüfe, ob das Team angemeldet ist oder auf der Warteliste steht
        team_registered = team_name in event["teams"]
        team_on_waitlist = False
        
        for i, (wl_team, _) in enumerate(event["waitlist"]):
            if wl_team == team_name:
                team_on_waitlist = True
                break
                
        if team_registered or team_on_waitlist:
            # Bestätigungsdialog anzeigen
            embed = discord.Embed(
                title="⚠️ Team wirklich abmelden?",
                description=f"Bist du sicher, dass du dein Team **{team_name}** abmelden möchtest?\n\n"
                           f"Diese Aktion kann nicht rückgängig gemacht werden!",
                color=discord.Color.red()
            )
            
            # Erstelle die Bestätigungsansicht
            view = TeamUnregisterConfirmationView(team_name, is_admin=False)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            
            # Log für Abmeldebestätigungsdialog
            status = "registriert" if team_registered else "auf der Warteliste"
            await send_to_log_channel(
                f"🔄 Abmeldungsprozess gestartet: {interaction.user.name} ({interaction.user.id}) will Team '{team_name}' abmelden (Status: {status})",
                level="INFO",
                guild=interaction.guild
            )
        else:
            await interaction.response.send_message(
                f"Team {team_name} ist weder angemeldet noch auf der Warteliste.",
                ephemeral=True
            )
            # Log für fehlgeschlagene Abmeldung
            await send_to_log_channel(
                f"⚠️ Abmeldungsversuch fehlgeschlagen: Team '{team_name}' von {interaction.user.name} ({interaction.user.id}) ist weder angemeldet noch auf der Warteliste",
                level="WARNING",
                guild=interaction.guild
            )
    
    # Die waitlist_callback-Methode wurde entfernt, da die Warteliste jetzt automatisch verwaltet wird
    
    async def team_info_callback(self, interaction: discord.Interaction):
        """Callback für Team-Info-Button"""
        # Sende eine ephemeral Nachricht mit Team-Informationen
        await interaction.response.defer(ephemeral=True)
        
        global user_team_assignments
        event = get_event()
        user_id = str(interaction.user.id)
        
        # Hole das Team des Users
        team_name = user_team_assignments.get(user_id)
        team_size = None
        if team_name and event and team_name in event["teams"]:
            team_size = event["teams"][team_name]
        
        if not team_name or not team_size:
            embed = discord.Embed(
                title="ℹ️ Team-Information",
                description="Du bist aktuell keinem Team zugewiesen.",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="Was kannst du tun?",
                value=f"• **Team erstellen**: Nutze den Button 'Team anmelden'\n"
                      f"• **Team beitreten**: Bitte den Teamleiter, dich einzuladen\n"
                      f"• **Hilfe erhalten**: Nutze `/help` für mehr Informationen",
                inline=False
            )
        else:
            embed = discord.Embed(
                title=f"👥 Team: {team_name}",
                description=f"Du bist Mitglied des Teams **{team_name}**.",
                color=discord.Color.green()
            )
            
            # Team-Größe
            embed.add_field(
                name="📊 Team-Größe",
                value=f"{team_size} {'Person' if team_size == 1 else 'Personen'}",
                inline=True
            )
            
            # Füge Event-Informationen hinzu
            if event:
                embed.add_field(
                    name="🎮 Event",
                    value=f"{event['name']} ({event['date']}, {event['time']})",
                    inline=False
                )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    async def edit_team_callback(self, interaction: discord.Interaction):
        """Callback für Team-Bearbeiten-Button"""
        user_id = str(interaction.user.id)
        
        # Verbesserte Rollenprüfung mit has_role (berücksichtigt ADMIN_IDs in DMs)
        is_admin = has_role(interaction.user, ORGANIZER_ROLE)
        is_clan_rep = has_role(interaction.user, CLAN_REP_ROLE)
        
        # Prüfe zuerst, ob es überhaupt ein aktives Event gibt
        event = get_event()
        if not event:
            await interaction.response.send_message(
                "Es gibt derzeit kein aktives Event.",
                ephemeral=True
            )
            await send_to_log_channel(
                f"⚠️ Team-Bearbeitungsversuch fehlgeschlagen: Kein aktives Event vorhanden (Benutzer: {interaction.user.name})",
                level="WARNING",
                guild=interaction.guild
            )
            return
        
        if is_admin:
            # Prüfe, ob es Teams gibt
            if not event["teams"] and not event["waitlist"]:
                await interaction.response.send_message(
                    "Es sind keine Teams zum Bearbeiten vorhanden.",
                    ephemeral=True
                )
                await send_to_log_channel(
                    f"ℹ️ Admin-Team-Bearbeitungsversuch fehlgeschlagen: Keine Teams vorhanden (Admin: {interaction.user.name})",
                    level="INFO",
                    guild=interaction.guild
                )
                return
                
            # Admins sehen alle Teams zur Auswahl
            view = AdminTeamSelector()
            await interaction.response.send_message(
                "Wähle das Team, das du bearbeiten möchtest:",
                view=view,
                ephemeral=True
            )
            
            # Log für Admin-Team-Bearbeitung
            await send_to_log_channel(
                f"👤 Admin-Teambearbeitungsprozess gestartet: {interaction.user.name} ({interaction.user.id}) wählt ein Team zur Bearbeitung",
                level="INFO",
                guild=interaction.guild
            )
        elif is_clan_rep:
            # Clan-Reps können ihr eigenes Team bearbeiten oder ein neues Team anmelden
            team_name = user_team_assignments.get(user_id)
            
            if not team_name:
                # Wenn kein Team zugewiesen ist, öffne das Formular für die Teamerstellung
                modal = TeamRegistrationModal(interaction.user)
                await interaction.response.send_modal(modal)
                
                # Log für Teamregistrierungsprozess
                await send_to_log_channel(
                    f"📋 Teamregistrierungsprozess gestartet: {interaction.user.name} ({interaction.user.id}) meldet ein neues Team an",
                    level="INFO",
                    guild=interaction.guild
                )
                return
            
            # Team ist zugewiesen, prüfe ob es angemeldet ist oder auf der Warteliste steht
            team_size = None
            is_on_waitlist = False
            
            if team_name in event["teams"]:
                team_size = event["teams"][team_name]
            else:
                for wl_team, wl_size in event["waitlist"]:
                    if wl_team == team_name:
                        team_size = wl_size
                        is_on_waitlist = True
                        break
            
            if team_size is None:
                await interaction.response.send_message(
                    f"Team {team_name} wurde nicht gefunden.",
                    ephemeral=True
                )
                await send_to_log_channel(
                    f"⚠️ Team-Bearbeitungsversuch fehlgeschlagen: Team '{team_name}' von {interaction.user.name} ({interaction.user.id}) nicht gefunden",
                    level="WARNING",
                    guild=interaction.guild
                )
                return
            
            # Öffne das Modal zur Teambearbeitung
            modal = TeamEditModal(team_name, team_size, event["max_team_size"])
            await interaction.response.send_modal(modal)
            
            # Log für Team-Bearbeitung
            status = "auf der Warteliste" if is_on_waitlist else "registriert"
            await send_to_log_channel(
                f"🔄 Team-Bearbeitungsprozess gestartet: {interaction.user.name} ({interaction.user.id}) bearbeitet Team '{team_name}' (Status: {status}, Aktuelle Größe: {team_size})",
                level="INFO",
                guild=interaction.guild
            )
        else:
            await interaction.response.send_message(
                f"Nur Mitglieder mit der Rolle '{CLAN_REP_ROLE}' oder '{ORGANIZER_ROLE}' können Teams bearbeiten.",
                ephemeral=True
            )
            # Log für unberechtigten Zugriff
            await send_to_log_channel(
                f"🚫 Unberechtigter Zugriffsversuch: {interaction.user.name} ({interaction.user.id}) hat versucht, ein Team zu bearbeiten ohne die erforderlichen Rollen",
                level="WARNING",
                guild=interaction.guild
            )
    
    async def admin_callback(self, interaction: discord.Interaction):
        """Callback für Admin-Button"""
        await interaction.response.defer(ephemeral=True)
        
        # Verbesserte Rollenprüfung mit has_role (berücksichtigt ADMIN_IDs in DMs)
        if not has_role(interaction.user, ORGANIZER_ROLE):
            await interaction.followup.send(
                f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können diese Aktion ausführen.",
                ephemeral=True
            )
            # Log für unberechtigten Zugriff
            await send_to_log_channel(
                f"🚫 Unberechtigter Zugriffsversuch: {interaction.user.name} ({interaction.user.id}) hat versucht, auf Admin-Funktionen zuzugreifen ohne die Rolle '{ORGANIZER_ROLE}'",
                level="WARNING",
                guild=interaction.guild
            )
            return
        
        # Prüfe, ob es ein aktives Event gibt
        event = get_event()
        if not event:
            await interaction.followup.send("Es gibt kein aktives Event.", ephemeral=True)
            await send_to_log_channel(
                f"⚠️ Admin-Zugriff bei fehlendem Event: {interaction.user.name} ({interaction.user.id}) hat versucht, auf Admin-Funktionen zuzugreifen, aber es gibt kein aktives Event",
                level="WARNING",
                guild=interaction.guild
            )
            return
        
        # Erstelle ein Embed mit Admin-Aktionen
        embed = discord.Embed(
            title="⚙️ Admin-Aktionen",
            description="Wähle eine der folgenden Aktionen:",
            color=discord.Color.dark_red()
        )
        
        # Erstelle ein View mit Admin-Aktionen
        view = AdminActionView()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        
        # Log für Admin-Panel-Zugriff
        await send_to_log_channel(
            f"👤 Admin-Panel geöffnet: {interaction.user.name} ({interaction.user.id}) hat das Admin-Panel für das Event '{event['name']}' geöffnet",
            level="INFO",
            guild=interaction.guild
        )

class AdminActionView(BaseView):
    """View mit Buttons für Admin-Aktionen"""
    def __init__(self):
        super().__init__(timeout=3600, title="Admin-Aktionen")  # 1 Stunde Timeout
        
        # Open Registration
        open_reg_button = ui.Button(
            label="Registrierung öffnen", 
            emoji="🔓",
            style=discord.ButtonStyle.primary,
            custom_id=f"admin_openreg"
        )
        open_reg_button.callback = self.open_reg_callback
        self.add_item(open_reg_button)
        
        # Manage Teams
        manage_teams_button = ui.Button(
            label="Teams verwalten", 
            emoji="👥",
            style=discord.ButtonStyle.primary,
            custom_id=f"admin_manage_teams"
        )
        manage_teams_button.callback = self.manage_teams_callback
        self.add_item(manage_teams_button)
        
        # Add Team Button
        add_team_button = ui.Button(
            label="Team hinzufügen", 
            emoji="➕",
            style=discord.ButtonStyle.success,
            custom_id=f"admin_add_team"
        )
        add_team_button.callback = self.add_team_callback
        self.add_item(add_team_button)
        
        # Remove Team Button
        remove_team_button = ui.Button(
            label="Team abmelden", 
            emoji="❌",
            style=discord.ButtonStyle.danger,
            custom_id=f"admin_remove_team"
        )
        remove_team_button.callback = self.remove_team_callback
        self.add_item(remove_team_button)
        
        # Delete Event
        delete_button = ui.Button(
            label="Event löschen", 
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            custom_id=f"admin_delete"
        )
        delete_button.callback = self.delete_callback
        self.add_item(delete_button)
    
    async def open_reg_callback(self, interaction: discord.Interaction):
        """Callback für Registrierung öffnen"""
        # Verhindere doppelte Antworten
        if self.check_response(interaction):
            await self.handle_already_responded(interaction)
            return
            
        # Überprüfe Berechtigung
        if not has_role(interaction.user, ORGANIZER_ROLE):
            await interaction.response.send_message(
                f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können diese Aktion ausführen.",
                ephemeral=True
            )
            # Log für unberechtigten Zugriff
            await send_to_log_channel(
                f"🚫 Unberechtigter Zugriffsversuch: {interaction.user.name} ({interaction.user.id}) hat versucht, die Registrierung zu öffnen ohne die Rolle '{ORGANIZER_ROLE}'",
                level="WARNING",
                guild=interaction.guild
            )
            return
        
        # Hole das aktive Event
        event = get_event()
        if not event:
            await interaction.response.send_message("Es gibt kein aktives Event.", ephemeral=True)
            await send_to_log_channel(
                f"⚠️ Registrierungsöffnung fehlgeschlagen: Kein aktives Event vorhanden (Admin: {interaction.user.name})",
                level="WARNING",
                guild=interaction.guild
            )
            return
        
        # Speichere die alte Teamgröße für das Logging
        old_max_size = event["max_team_size"]
        
        # Aktualisiere die maximale Teamgröße
        event["max_team_size"] = EXPANDED_MAX_TEAM_SIZE
        save_data(event_data, channel_id, user_team_assignments)
        
        embed = discord.Embed(
            title="🔓 Maximale Teamgröße erhöht",
            description=f"Die maximale Teamgröße wurde auf {EXPANDED_MAX_TEAM_SIZE} erhöht.",
            color=discord.Color.green()
        )
        
        # Benachrichtige auch im öffentlichen Channel
        channel = bot.get_channel(interaction.channel_id)
        if channel:
            await channel.send(
                f"📢 **Ankündigung**: Die maximale Teamgröße für das Event '{event['name']}' "
                f"wurde auf {EXPANDED_MAX_TEAM_SIZE} erhöht!"
            )
        
        # Log für erfolgreiche Registrierungsöffnung
        await send_to_log_channel(
            f"🔓 Registrierung geöffnet: {interaction.user.name} ({interaction.user.id}) hat die maximale Teamgröße von {old_max_size} auf {EXPANDED_MAX_TEAM_SIZE} erhöht für Event '{event['name']}'",
            level="INFO",
            guild=interaction.guild
        )
            
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    async def manage_teams_callback(self, interaction: discord.Interaction):
        """Callback für Team-Verwaltung"""
        # Verhindere doppelte Antworten
        if self.check_response(interaction):
            await self.handle_already_responded(interaction)
            return
            
        # Überprüfe Berechtigung
        if not has_role(interaction.user, ORGANIZER_ROLE):
            await interaction.response.send_message(
                f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können diese Aktion ausführen.",
                ephemeral=True
            )
            return
        
        # Erstelle ein Embed mit der Team-Übersicht
        event = get_event()
        if not event:
            await interaction.response.send_message("Es gibt kein aktives Event.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="👥 Team-Verwaltung",
            description=f"Hier kannst du alle Teams für das Event **{event['name']}** verwalten.",
            color=discord.Color.blue()
        )
        
        # Angemeldete Teams
        teams_text = ""
        if event["teams"]:
            for team_name, size in event["teams"].items():
                teams_text += f"• **{team_name}**: {size} {'Person' if size == 1 else 'Personen'}\n"
        else:
            teams_text = "Noch keine Teams angemeldet."
        
        embed.add_field(
            name=f"📋 Angemeldete Teams ({len(event['teams'])})",
            value=teams_text,
            inline=False
        )
        
        # Warteliste
        if event["waitlist"]:
            waitlist_text = ""
            for i, (team_name, size) in enumerate(event["waitlist"]):
                waitlist_text += f"{i+1}. **{team_name}**: {size} {'Person' if size == 1 else 'Personen'}\n"
            
            embed.add_field(
                name=f"⏳ Warteliste ({len(event['waitlist'])})",
                value=waitlist_text,
                inline=False
            )
        
        # Erstelle die Team-Auswahl
        view = AdminTeamSelector()
        
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True
        )
    
    async def add_team_callback(self, interaction: discord.Interaction):
        """Callback zum Hinzufügen eines Teams"""
        # Verhindere doppelte Antworten
        if self.check_response(interaction, store_msg=False):
            await self.handle_already_responded(interaction)
            return
            
        # Überprüfe Berechtigung
        if not has_role(interaction.user, ORGANIZER_ROLE):
            await interaction.response.send_message(
                f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können diese Aktion ausführen.",
                ephemeral=True
            )
            return
        
        # Öffne ein Modal zum Hinzufügen eines Teams
        modal = AdminTeamCreateModal()
        await interaction.response.send_modal(modal)
    
    async def remove_team_callback(self, interaction: discord.Interaction):
        """Callback für Team abmelden"""
        # Verhindere doppelte Antworten
        if self.check_response(interaction):
            await self.handle_already_responded(interaction)
            return
            
        # Überprüfe Berechtigung
        if not has_role(interaction.user, ORGANIZER_ROLE):
            await interaction.response.send_message(
                f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können diese Aktion ausführen.",
                ephemeral=True
            )
            return
        
        # Hole das aktive Event
        event = get_event()
        if not event:
            await interaction.response.send_message("Es gibt kein aktives Event.", ephemeral=True)
            return
        
        # Zeige eine Team-Auswahl an
        embed = discord.Embed(
            title="❌ Team abmelden",
            description="Wähle ein Team aus, das du abmelden möchtest.",
            color=discord.Color.red()
        )
        
        # Erstelle die Team-Auswahl mit for_removal=True
        view = AdminTeamSelector(for_removal=True)
        
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True
        )
        
    async def delete_callback(self, interaction: discord.Interaction):
        """Callback für Event löschen"""
        # Verhindere doppelte Antworten
        if self.check_response(interaction):
            await self.handle_already_responded(interaction)
            return
            
        # Überprüfe Berechtigung
        if not has_role(interaction.user, ORGANIZER_ROLE):
            await interaction.response.send_message(
                f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können diese Aktion ausführen.",
                ephemeral=True
            )
            return
        
        # Hole das aktive Event
        event = get_event()
        if not event:
            await interaction.response.send_message("Es gibt kein aktives Event.", ephemeral=True)
            return
        
        # Zeige eine Bestätigungsanfrage
        embed = discord.Embed(
            title="⚠️ Event wirklich löschen?",
            description=f"Bist du sicher, dass du das Event **{event['name']}** löschen möchtest?\n\n"
                        f"Diese Aktion kann nicht rückgängig gemacht werden! Alle Team-Anmeldungen und Wartelisten-Einträge werden gelöscht.",
            color=discord.Color.red()
        )
        
        view = DeleteConfirmationView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class TeamUnregisterConfirmationView(BaseConfirmationView):
    """View für die Bestätigung einer Team-Abmeldung"""
    def __init__(self, team_name, is_admin=False):
        super().__init__(title="Team-Abmeldung")
        self.team_name = team_name.strip() if team_name else ""  # Behalte Originalschreibweise
        self.team_name_lower = team_name.strip().lower() if team_name else ""  # Lowercase für Vergleiche
        self.is_admin = is_admin
    
    @ui.button(label="Ja, Team abmelden", style=discord.ButtonStyle.danger)
    async def confirm_callback(self, interaction: discord.Interaction, button: ui.Button):
        """Callback für Bestätigung der Team-Abmeldung"""
        if not self.team_name:
            await interaction.response.send_message(
                "Fehler: Kein Team-Name angegeben.", 
                ephemeral=True
            )
            return
        
        # Verhindere doppelte Antworten
        if self.check_response(interaction):
            try:
                await interaction.followup.send(
                    "Diese Aktion wird bereits verarbeitet...",
                    ephemeral=True
                )
            except Exception:
                pass  # Ignoriere Fehler hier, um andere Funktionalität nicht zu beeinträchtigen
            return
        
        # Deaktiviere die Buttons, um Doppelklicks zu verhindern
        for child in self.children:
            child.disabled = True
        
        # Warte-Nachricht senden
        await interaction.response.edit_message(
            content="⏳ Verarbeite Team-Abmeldung...", 
            view=self
        )
        
        try:
            # Hole Event-Daten, um Team-Gesamtgröße zu ermitteln (angemeldet + Warteliste)
            event = get_event()
            total_size = 0
            registered_size = 0
            waitlist_size = 0
            
            if event:
                # Größe im registrierten Team (case-insensitive)
                for reg_team, reg_size in event.get("teams", {}).items():
                    if reg_team.lower() == self.team_name_lower:
                        registered_size = reg_size
                        total_size += registered_size
                        break
                
                # Größe auf der Warteliste (case-insensitive)
                for wl_team, wl_size in event.get("waitlist", []):
                    if wl_team.lower() == self.team_name_lower:
                        waitlist_size = wl_size
                        total_size += waitlist_size
                        break
            
            # Führe die Teamgrößenänderung auf 0 durch (was zur Abmeldung führt)
            success = await update_team_size(
                interaction, 
                self.team_name, 
                0, 
                is_admin=self.is_admin,
                reason="Team manuell abgemeldet"
            )
            
            if success:
                # Erfolgsnachricht mit vollständiger Teamgröße
                size_info = ""
                if registered_size > 0 and waitlist_size > 0:
                    size_info = f" ({registered_size} angemeldet, {waitlist_size} auf Warteliste, {total_size} insgesamt)"
                elif registered_size > 0:
                    size_info = f" ({registered_size} Spieler)"
                elif waitlist_size > 0:
                    size_info = f" ({waitlist_size} Spieler auf Warteliste)"
                
                embed = discord.Embed(
                    title="✅ Team abgemeldet",
                    description=f"Das Team **{self.team_name}**{size_info} wurde erfolgreich abgemeldet.",
                    color=discord.Color.green()
                )
                
                # Aktualisiere die Nachricht (nicht neue Antwort senden!)
                await interaction.edit_original_response(content=None, embed=embed, view=None)
                
                # Logging
                await send_to_log_channel(
                    f"✅ Team abgemeldet: Team '{self.team_name}'{size_info} wurde erfolgreich abgemeldet " + 
                    f"durch {'Admin' if self.is_admin else 'Benutzer'} {interaction.user.name}",
                    guild=interaction.guild
                )
            else:
                # Fehlermeldung
                embed = discord.Embed(
                    title="❌ Fehler",
                    description=f"Team {self.team_name} konnte nicht abgemeldet werden.",
                    color=discord.Color.red()
                )
                # Aktualisiere die Nachricht (nicht neue Antwort senden!)
                await interaction.edit_original_response(content=None, embed=embed, view=None)
                
                # Logging
                await send_to_log_channel(
                    f"❌ Fehler bei Abmeldung: Team '{self.team_name}' konnte nicht abgemeldet werden " + 
                    f"durch {'Admin' if self.is_admin else 'Benutzer'} {interaction.user.name}",
                    level="ERROR",
                    guild=interaction.guild
                )
        except Exception as e:
            # Fehlerbehandlung
            error_msg = str(e)
            logger.error(f"Fehler bei Bestätigung der Team-Abmeldung: {error_msg}")
            
            try:
                # Versuche, die ursprüngliche Nachricht zu aktualisieren
                error_embed = discord.Embed(
                    title="❌ Fehler bei der Team-Abmeldung",
                    description=f"Ein unerwarteter Fehler ist aufgetreten. Bitte versuche es später erneut.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(content=None, embed=error_embed, view=None)
            except Exception:
                # Falls das nicht klappt, ignoriere den Fehler
                pass
    
    @ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel_callback(self, interaction: discord.Interaction, button: ui.Button):
        """Callback für Abbruch der Team-Abmeldung"""
        # Verhindere doppelte Antworten
        if self.check_response(interaction):
            try:
                await interaction.followup.send(
                    "Diese Aktion wird bereits verarbeitet...",
                    ephemeral=True
                )
            except Exception:
                pass  # Ignoriere Fehler hier
            return
        
        # Deaktiviere die Buttons, um Doppelklicks zu verhindern
        for child in self.children:
            child.disabled = True
            
        # Log für abgebrochene Team-Abmeldung
        admin_or_user = "Admin" if self.is_admin else "Benutzer"
        await send_to_log_channel(
            f"🛑 Team-Abmeldung abgebrochen: {admin_or_user} {interaction.user.name} ({interaction.user.id}) hat die Abmeldung von Team '{self.team_name}' abgebrochen",
            level="INFO",
            guild=interaction.guild
        )
        
        embed = discord.Embed(
            title="🛑 Abmeldung abgebrochen",
            description=f"Die Abmeldung des Teams {self.team_name} wurde abgebrochen.",
            color=discord.Color.blue()
        )
        
        # Aktualisiere die Nachricht statt neue zu senden
        await interaction.response.edit_message(content=None, embed=embed, view=self)


class DeleteConfirmationView(BaseConfirmationView):
    """View für die Bestätigung einer Event-Löschung"""
    def __init__(self):
        super().__init__(title="Event-Löschung")
    
    @ui.button(label="Ja, Event löschen", style=discord.ButtonStyle.danger)
    async def confirm_callback(self, interaction: discord.Interaction, button: ui.Button):
        """Callback für Bestätigung der Löschung"""
        global event_data, user_team_assignments
        
        # Verhindere doppelte Antworten
        if self.check_response(interaction):
            try:
                await interaction.followup.send(
                    "Diese Aktion wird bereits verarbeitet...",
                    ephemeral=True
                )
            except Exception:
                pass
            return
        
        # Deaktiviere Buttons
        for child in self.children:
            child.disabled = True
        
        # Warte-Nachricht senden
        await interaction.response.edit_message(
            content="⏳ Verarbeite Event-Löschung...", 
            view=self
        )
        
        try:
            # Lösche das Event
            event = get_event()
            if event:
                event_name = event['name']
                event_date = event.get('date', 'unbekannt')
                registered_teams = len(event["teams"])
                waitlist_teams = len(event["waitlist"])
                
                # Erstelle ein Log mit detaillierten Informationen zum Event
                log_message = (
                    f"🗑️ Event gelöscht: {interaction.user.name} ({interaction.user.id}) hat das Event '{event_name}' gelöscht\n"
                    f"Datum: {event_date}, Angemeldete Teams: {registered_teams}, Teams auf der Warteliste: {waitlist_teams}"
                )
                await send_to_log_channel(log_message, level="WARNING", guild=interaction.guild)
                
                # Jetzt löschen
                event_data.clear()
                user_team_assignments.clear()
                save_data(event_data, channel_id, user_team_assignments)
                
                embed = discord.Embed(
                    title="✅ Event gelöscht",
                    description="Das Event wurde erfolgreich gelöscht.",
                    color=discord.Color.green()
                )
                
                # Aktualisiere die Bestätigungsnachricht
                await interaction.edit_original_response(content=None, embed=embed, view=None)
                
                # Benachrichtige auch im öffentlichen Channel
                channel = bot.get_channel(interaction.channel_id)
                if channel:
                    await channel.send(f"📢 **Information**: Das Event '{event_name}' wurde gelöscht.")
            else:
                embed = discord.Embed(
                    title="❌ Fehler",
                    description="Es gibt kein aktives Event zum Löschen.",
                    color=discord.Color.red()
                )
                
                await send_to_log_channel(
                    f"⚠️ Event-Löschungsversuch fehlgeschlagen: Kein aktives Event vorhanden (Admin: {interaction.user.name})",
                    level="WARNING", 
                    guild=interaction.guild
                )
                
                # Aktualisiere die Bestätigungsnachricht
                await interaction.edit_original_response(content=None, embed=embed, view=None)
        except Exception as e:
            logger.error(f"Fehler bei Event-Löschung: {e}")
            try:
                error_embed = discord.Embed(
                    title="❌ Fehler bei der Event-Löschung",
                    description=f"Ein unerwarteter Fehler ist aufgetreten. Bitte versuche es später erneut.",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(content=None, embed=error_embed, view=None)
            except Exception:
                pass
    
    @ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel_callback(self, interaction: discord.Interaction, button: ui.Button):
        """Callback für Abbruch der Löschung"""
        # Verhindere doppelte Antworten
        if self.check_response(interaction):
            try:
                await interaction.followup.send(
                    "Diese Aktion wird bereits verarbeitet...",
                    ephemeral=True
                )
            except Exception:
                pass
            return
        
        # Deaktiviere die Buttons
        for child in self.children:
            child.disabled = True
        
        # Hole das aktive Event für Logging
        event = get_event()
        if event:
            event_name = event['name']
            # Log für abgebrochene Event-Löschung
            await send_to_log_channel(
                f"🛑 Event-Löschung abgebrochen: {interaction.user.name} ({interaction.user.id}) hat die Löschung von Event '{event_name}' abgebrochen",
                level="INFO",
                guild=interaction.guild
            )
        
        embed = discord.Embed(
            title="🛑 Löschung abgebrochen",
            description="Die Löschung des Events wurde abgebrochen.",
            color=discord.Color.blue()
        )
        
        # Aktualisiere die Nachricht statt neue zu senden
        await interaction.response.edit_message(content=None, embed=embed, view=self)

async def send_team_dm_notification(team_name, message):
    """
    Sendet eine DM-Benachrichtigung an den Teamleiter.
    
    Parameters:
    - team_name: Name des Teams
    - message: Nachricht, die gesendet werden soll
    """
    # Suche nach dem Benutzer, der das Team erstellt hat (case-insensitive)
    team_name_lower = team_name.lower() if team_name else ""
    team_leader_id = None
    for uid, tname in user_team_assignments.items():
        if tname.lower() == team_name_lower:
            team_leader_id = uid
            break
    
    if team_leader_id:
        try:
            # Versuche, den Benutzer zu erreichen
            user = await bot.fetch_user(int(team_leader_id))
            if user:
                await user.send(message)
                logger.info(f"DM Benachrichtigung an {user.name} für Team {team_name} gesendet")
        except discord.errors.Forbidden:
            logger.warning(f"Konnte keine DM an Benutzer {team_leader_id} senden (Team {team_name})")
        except Exception as e:
            logger.error(f"Fehler beim Senden der DM an Benutzer {team_leader_id}: {e}")


async def update_team_size(interaction, team_name, new_size, is_admin=False, reason=None):
    """
    Aktualisiert die Größe eines Teams und verwaltet die Warteliste entsprechend.
    Behandelt Teams als Einheit, unabhängig von Event/Warteliste-Platzierung.
    
    Parameters:
    - interaction: Discord-Interaktion
    - team_name: Name des Teams
    - new_size: Neue Teamgröße
    - is_admin: Ob die Änderung von einem Admin durchgeführt wird
    - reason: Optionaler Grund für die Änderung (nur für Admins)
    
    Returns:
    - True bei Erfolg, False bei Fehler
    """
    # Defensive Programmierung - Validiere Eingaben
    if not isinstance(team_name, str) or not team_name.strip():
        logger.error(f"Ungültiger Team-Name: {team_name}")
        await interaction.response.send_message(
            "Ungültiger Team-Name.",
            ephemeral=True
        )
        return False
    
    team_name = team_name.strip().lower()  # Normalisiere Teamnamen (Case-insensitive)
    
    try:
        new_size = int(new_size)
    except (ValueError, TypeError):
        logger.error(f"Ungültige Teamgröße: {new_size}")
        await interaction.response.send_message(
            "Die Teamgröße muss eine ganze Zahl sein.",
            ephemeral=True
        )
        return False
    
    event = get_event()
    if not event:
        await interaction.response.send_message(
            "Es gibt derzeit kein aktives Event.",
            ephemeral=True
        )
        return False
    
    user_id = str(interaction.user.id)
    
    # Prüfe Berechtigungen für Nicht-Admins
    if not is_admin:
        # Prüfe, ob der Nutzer die CLAN_REP_ROLLE hat
        is_clan_rep = has_role(interaction.user, CLAN_REP_ROLE)
        if not is_clan_rep:
            await interaction.response.send_message(
                f"Nur Mitglieder mit der Rolle '{CLAN_REP_ROLE}' können Teams bearbeiten.",
                ephemeral=True
            )
            return False
            
        # Prüfe, ob der Nutzer bereits einem Team zugewiesen ist
        user_team = user_team_assignments.get(user_id, "").lower()
        if user_team and user_team != team_name:
            await interaction.response.send_message(
                "Du kannst nur dein eigenes Team bearbeiten.",
                ephemeral=True
            )
            return False
    
    max_team_size = event.get("max_team_size", 0)
    
    # Validiere neue Teamgröße
    if new_size < 0:
        await interaction.response.send_message(
            "Die Teamgröße kann nicht negativ sein.",
            ephemeral=True
        )
        return False
    
    if new_size > max_team_size and not is_admin:
        await interaction.response.send_message(
            f"Die maximale Teamgröße beträgt {max_team_size}.",
            ephemeral=True
        )
        return False
    
    # Hole alle aktuellen Daten des Teams (Event + Warteliste)
    event_size, waitlist_size, current_total_size, registered_name, waitlist_entries = get_team_total_size(event, team_name)
    
    # Falls waitlist_entries vorhanden sind, für die alte Logik kompatibel machen
    waitlist_team_name = None
    waitlist_index = -1
    if waitlist_entries:
        # Nimm den ersten Eintrag für die Kompatibilität
        first_entry = waitlist_entries[0]
        waitlist_index = first_entry[0]  # Index
        waitlist_team_name = first_entry[1]  # Team-Name
    
    # Prüfe, ob das Team existiert
    if current_total_size == 0 and new_size > 0:
        # Neues Team anlegen
        # Weise den aktuellen Benutzer dem Team zu
        user_id = str(interaction.user.id)
        user_team_assignments[user_id] = team_name
        
        # Füge das Team zum Event oder zur Warteliste hinzu (wird später erledigt)
        logger.info(f"Neues Team '{team_name}' wird mit Größe {new_size} erstellt")
        # Fahre mit der normalen Logik fort
    
    # Wenn Teamgröße 0 ist, Team automatisch abmelden
    if new_size == 0:
        # Entferne Team aus Event und Warteliste
        if event_size > 0:
            # Nur exakt diesen Teamnamen entfernen (case-sensitive Lookup im Dict)
            for registered_name in list(event["teams"].keys()):
                if registered_name.lower() == team_name:
                    registered_size = event["teams"].pop(registered_name)
                    event["slots_used"] -= registered_size
                    break
        
        # Entferne von Warteliste (case-insensitive)
        if waitlist_size > 0:
            waitlist_indices_to_remove = []
            for i, (wl_team, wl_size) in enumerate(event["waitlist"]):
                if wl_team.lower() == team_name:
                    waitlist_indices_to_remove.append(i)
            
            # Von hinten nach vorne entfernen, um Indizes nicht zu verschieben
            for i in sorted(waitlist_indices_to_remove, reverse=True):
                event["waitlist"].pop(i)
        
        # Statustext für Nachricht erstellen
        total_size_message = ""
        if event_size > 0 and waitlist_size > 0:
            total_size_message = f"mit {event_size} angemeldeten Spielern und {waitlist_size} auf der Warteliste (insgesamt {current_total_size})"
        elif event_size > 0:
            total_size_message = f"mit {event_size} angemeldeten Spielern"
        elif waitlist_size > 0:
            total_size_message = f"mit {waitlist_size} Spielern auf der Warteliste"
        
        # Finde alle Benutzer, die diesem Team zugewiesen sind, und entferne sie (case-insensitive)
        users_to_remove = []
        for uid, tname in user_team_assignments.items():
            if tname.lower() == team_name:
                users_to_remove.append(uid)
        
        for uid in users_to_remove:
            del user_team_assignments[uid]
            
        save_data(event_data, channel_id, user_team_assignments)
        
        # Freie Slots für die Warteliste verwenden, wenn Team angemeldet war
        if event_size > 0:
            await process_waitlist_after_change(interaction, event_size)
        
        # Log für Team-Abmeldung
        admin_or_user = "Admin" if is_admin else "Benutzer"
        admin_name = getattr(interaction.user, "name", "Unbekannt")
        log_message = f"❌ Team abgemeldet: {admin_or_user} {admin_name} hat Team '{team_name}' {total_size_message} abgemeldet"
        if reason:
            log_message += f" (Grund: {reason})"
        await send_to_log_channel(log_message, guild=interaction.guild)
        
        # Nachricht senden
        message = f"Team {team_name} {total_size_message} wurde abgemeldet."
        if reason:
            message += f" Grund: {reason}"
            
        # Nutze followup bei modals/views, ansonsten response
        try:
            if hasattr(interaction, 'edit_original_response'):
                embed = discord.Embed(
                    title="✅ Team abgemeldet",
                    description=message,
                    color=discord.Color.green()
                )
                await interaction.edit_original_response(content=None, embed=embed, view=None)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception as e:
            logger.error(f"Fehler beim Senden der Abmeldebestätigung: {e}")
            try:
                await interaction.followup.send(message, ephemeral=True)
            except Exception:
                pass
        
        # Sende DM an Teamleiter bei Admin-Änderungen
        if is_admin:
            dm_message = f"❌ Dein Team **{team_name}** {total_size_message} wurde von einem Administrator abgemeldet."
            if reason:
                dm_message += f"\nGrund: {reason}"
            
            dm_message += f"\n\nFalls du Fragen hast, wende dich bitte an einen Administrator."
            await send_team_dm_notification(team_name, dm_message)
        
        # Channel aktualisieren
        if channel_id:
            channel = bot.get_channel(interaction.channel_id)
            if channel:
                await send_event_details(channel)
        
        return True
    
    # Berechne Größenänderung basierend auf Gesamtgröße
    size_difference = new_size - current_total_size
    
    # Keine Änderung in der Gesamtgröße
    if size_difference == 0:
        await interaction.response.send_message(
            f"Die Gesamtgröße von Team {team_name} bleibt unverändert bei {current_total_size} " +
            f"({event_size} angemeldet, {waitlist_size} auf der Warteliste).",
            ephemeral=True
        )
        return True
    
    # 1. FALL: Erhöhung der Teamgröße
    if size_difference > 0:
        # Berechne verfügbare Slots im Event
        available_slots = event["max_slots"] - event["slots_used"]
        
        # Finde den richtigen Teamnamen im Dictionary (case-sensitive lookup)
        registered_team_name = None
        for name in event["teams"]:
            if name.lower() == team_name:
                registered_team_name = name
                break
        
        # Finde den richtigen Teamnamen in der Warteliste
        waitlist_team_name = None
        waitlist_index = -1
        for i, (wl_team, wl_size) in enumerate(event["waitlist"]):
            if wl_team.lower() == team_name:
                waitlist_team_name = wl_team
                waitlist_index = i
                break
                
        # Priorität: Erst Event-Slots füllen, dann Warteliste
        if size_difference <= available_slots:
            # Genug freie Slots im Event - alles kann in den Event-Slots untergebracht werden
            if registered_team_name:
                # Team bereits registriert - erhöhe die Größe
                event["teams"][registered_team_name] += size_difference
                event["slots_used"] += size_difference
            else:
                # Team nicht registriert - erstelle es
                registered_team_name = team_name
                event["teams"][registered_team_name] = size_difference
                event["slots_used"] += size_difference
            
            # Log für Teamgröße-Erhöhung
            admin_or_user = "Admin" if is_admin else "Benutzer"
            admin_name = getattr(interaction.user, "name", "Unbekannt")
            log_message = f"📈 Teamgröße erhöht: {admin_or_user} {admin_name} hat die Größe von Team '{team_name}' von {current_total_size} auf {new_size} erhöht"
            if reason:
                log_message += f" (Grund: {reason})"
            await send_to_log_channel(log_message, guild=interaction.guild)
            
            # Nachricht senden
            event_addition = size_difference
            await interaction.response.send_message(
                f"Die Teamgröße von {team_name} wurde von {current_total_size} auf {new_size} erhöht. " +
                f"{event_addition} Spieler wurden zum Event hinzugefügt.",
                ephemeral=True
            )
            
            # Sende DM bei Admin-Änderungen
            if is_admin:
                dm_message = f"📈 Die Größe deines Teams **{team_name}** wurde von einem Administrator von {current_total_size} auf {new_size} erhöht."
                if reason:
                    dm_message += f"\nGrund: {reason}"
                
                dm_message += f"\n\nFalls du Fragen hast, wende dich bitte an einen Administrator."
                await send_team_dm_notification(team_name, dm_message)
        else:
            # Nicht genug Plätze im Event - fülle Event-Slots, Rest auf Warteliste
            # Zuerst Event-Slots füllen
            event_addition = available_slots
            waitlist_addition = size_difference - available_slots
            
            if registered_team_name:
                # Team bereits registriert - erhöhe die Größe
                event["teams"][registered_team_name] += event_addition
                event["slots_used"] += event_addition
            else:
                # Team nicht registriert - erstelle es
                registered_team_name = team_name
                event["teams"][registered_team_name] = event_addition
                event["slots_used"] += event_addition
            
            # Dann Warteliste aktualisieren/erstellen
            if waitlist_team_name:
                # Team bereits auf Warteliste - erhöhe die Größe
                new_waitlist_size = waitlist_size + waitlist_addition
                event["waitlist"][waitlist_index] = (waitlist_team_name, new_waitlist_size)
                waitlist_message = f"{waitlist_addition} Spieler wurden zur Warteliste hinzugefügt (jetzt {new_waitlist_size})."
            else:
                # Team nicht auf Warteliste - füge es hinzu
                event["waitlist"].append((team_name, waitlist_addition))
                waitlist_message = f"{waitlist_addition} Spieler wurden auf die Warteliste gesetzt (Position {len(event['waitlist'])})."
            
            # Log für Teamgröße-Erhöhung mit Warteliste
            admin_or_user = "Admin" if is_admin else "Benutzer"
            admin_name = getattr(interaction.user, "name", "Unbekannt")
            log_message = f"📈 Teamgröße erhöht: {admin_or_user} {admin_name} hat die Größe von Team '{team_name}' von {current_total_size} auf {new_size} erhöht (Event +{event_addition}, Warteliste +{waitlist_addition})"
            if reason:
                log_message += f" (Grund: {reason})"
            await send_to_log_channel(log_message, guild=interaction.guild)
            
            # Nachricht senden
            await interaction.response.send_message(
                f"Die Teamgröße von {team_name} wurde von {current_total_size} auf {new_size} erhöht. " +
                f"{event_addition} Spieler wurden zum Event hinzugefügt. {waitlist_message}",
                ephemeral=True
            )
            
            # Sende DM bei Admin-Änderungen
            if is_admin:
                dm_message = f"📈 Die Größe deines Teams **{team_name}** wurde von einem Administrator von {current_total_size} auf {new_size} erhöht. " + \
                            f"{event_addition} Spieler wurden zum Event hinzugefügt und {waitlist_addition} Spieler auf die Warteliste gesetzt."
                if reason:
                    dm_message += f"\nGrund: {reason}"
                
                dm_message += f"\n\nFalls du Fragen hast, wende dich bitte an einen Administrator."
                await send_team_dm_notification(team_name, dm_message)
    
    # 2. FALL: Verringerung der Teamgröße
    else:  # size_difference < 0
        # Absolute Größe der Reduktion
        reduction = abs(size_difference)
        
        # Priorität: Erst Warteliste reduzieren, dann Event-Slots
        waitlist_reduction = min(waitlist_size, reduction)
        event_reduction = reduction - waitlist_reduction
        
        # Finde den richtigen Teamnamen im Dictionary (case-sensitive lookup)
        registered_team_name = None
        for name in event["teams"]:
            if name.lower() == team_name:
                registered_team_name = name
                break
        
        # Finde den richtigen Teamnamen in der Warteliste
        waitlist_team_name = None
        waitlist_index = -1
        for i, (wl_team, wl_size) in enumerate(event["waitlist"]):
            if wl_team.lower() == team_name:
                waitlist_team_name = wl_team
                waitlist_index = i
                break
        
        # Erst Warteliste reduzieren
        if waitlist_reduction > 0 and waitlist_team_name and waitlist_index >= 0:
            new_waitlist_size = waitlist_size - waitlist_reduction
            if new_waitlist_size > 0:
                # Aktualisiere Warteliste
                event["waitlist"][waitlist_index] = (waitlist_team_name, new_waitlist_size)
            else:
                # Entferne von Warteliste
                event["waitlist"].pop(waitlist_index)
        
        # Dann Event-Slots reduzieren, falls nötig
        if event_reduction > 0 and registered_team_name:
            new_event_size = event_size - event_reduction
            if new_event_size > 0:
                # Aktualisiere Event-Slots
                event["teams"][registered_team_name] = new_event_size
                event["slots_used"] -= event_reduction
            else:
                # Entferne aus Event
                event["slots_used"] -= event["teams"].pop(registered_team_name)
        
        # Log für Teamgröße-Verringerung
        admin_or_user = "Admin" if is_admin else "Benutzer"
        admin_name = getattr(interaction.user, "name", "Unbekannt")
        log_message = f"📉 Teamgröße verringert: {admin_or_user} {admin_name} hat die Größe von Team '{team_name}' von {current_total_size} auf {new_size} verringert"
        if waitlist_reduction > 0 and event_reduction > 0:
            log_message += f" (Warteliste -{waitlist_reduction}, Event -{event_reduction})"
        elif waitlist_reduction > 0:
            log_message += f" (nur Warteliste -{waitlist_reduction})"
        elif event_reduction > 0:
            log_message += f" (nur Event -{event_reduction})"
        
        if reason:
            log_message += f" (Grund: {reason})"
        await send_to_log_channel(log_message, guild=interaction.guild)
        
        # Nachricht für Benutzer erstellen
        message = f"Die Teamgröße von {team_name} wurde von {current_total_size} auf {new_size} verringert."
        if waitlist_reduction > 0 and event_reduction > 0:
            message += f" Es wurden {waitlist_reduction} Spieler von der Warteliste und {event_reduction} Spieler vom Event entfernt."
        elif waitlist_reduction > 0:
            message += f" Es wurden {waitlist_reduction} Spieler von der Warteliste entfernt."
        elif event_reduction > 0:
            message += f" Es wurden {event_reduction} Spieler vom Event entfernt."
        
        await interaction.response.send_message(message, ephemeral=True)
        
        # Sende DM bei Admin-Änderungen
        if is_admin:
            dm_message = f"📉 Die Größe deines Teams **{team_name}** wurde von einem Administrator von {current_total_size} auf {new_size} verringert."
            if waitlist_reduction > 0 and event_reduction > 0:
                dm_message += f" Es wurden {waitlist_reduction} Spieler von der Warteliste und {event_reduction} Spieler vom Event entfernt."
            elif waitlist_reduction > 0:
                dm_message += f" Es wurden {waitlist_reduction} Spieler von der Warteliste entfernt."
            elif event_reduction > 0:
                dm_message += f" Es wurden {event_reduction} Spieler vom Event entfernt."
            
            if reason:
                dm_message += f"\nGrund: {reason}"
            
            dm_message += f"\n\nFalls du Fragen hast, wende dich bitte an einen Administrator."
            await send_team_dm_notification(team_name, dm_message)
        
        # Freie Event-Slots für Teams auf der Warteliste nutzen
        if event_reduction > 0:
            await process_waitlist_after_change(interaction, event_reduction)
    
    # Speichere die Änderungen
    save_data(event_data, channel_id, user_team_assignments)
    
    # Aktualisiere die Event-Anzeige im Channel
    if channel_id:
        channel = bot.get_channel(interaction.channel_id)
        if channel:
            await send_event_details(channel)
    
    return True

async def process_waitlist_after_change(interaction, free_slots):
    """
    Verarbeitet die Warteliste, nachdem Slots frei geworden sind.
    
    Parameters:
    - interaction: Discord-Interaktion
    - free_slots: Anzahl der frei gewordenen Slots
    """
    event = get_event()
    if not event or free_slots <= 0 or not event["waitlist"]:
        return
    
    update_needed = False
    processed_teams = []
    
    while free_slots > 0 and event["waitlist"]:
        team_name, size = event["waitlist"][0]
        
        if size <= free_slots:
            # Das komplette Team kann nachrücken
            event["waitlist"].pop(0)
            event["slots_used"] += size
            event["teams"][team_name] = event["teams"].get(team_name, 0) + size
            free_slots -= size
            update_needed = True
            processed_teams.append((team_name, size))
        elif free_slots > 0:
            # Nur ein Teil des Teams kann nachrücken
            event["waitlist"][0] = (team_name, size - free_slots)
            event["slots_used"] += free_slots
            event["teams"][team_name] = event["teams"].get(team_name, 0) + free_slots
            processed_teams.append((team_name, free_slots))
            free_slots = 0
            update_needed = True
    
    if update_needed:
        save_data(event_data, channel_id, user_team_assignments)
        
        # Log für verarbeitete Warteliste
        if interaction and interaction.guild:
            initiator_name = getattr(interaction.user, "name", "System")
            log_message = f"⏫ Warteliste verarbeitet: {len(processed_teams)} Teams aufgerückt (initiiert von {initiator_name})"
            await send_to_log_channel(log_message, guild=interaction.guild)
        
        # Benachrichtigungen für aufgerückte Teams
        for team_name, moved_size in processed_teams:
            # Channel-Benachrichtigung
            if channel_id:
                channel = bot.get_channel(interaction.channel_id)
                if channel:
                    if moved_size == event["teams"][team_name]:
                        await channel.send(f"📢 Team {team_name} wurde komplett von der Warteliste in die Anmeldung aufgenommen!")
                    else:
                        await channel.send(f"📢 {moved_size} Spieler von Team {team_name} wurden von der Warteliste in die Anmeldung aufgenommen!")
            
            # Log für jedes aufgerückte Team
            if interaction and interaction.guild:
                team_log = f"📋 Team '{team_name}': {moved_size} Mitglieder von der Warteliste aufgerückt"
                await send_to_log_channel(team_log, level="INFO", guild=interaction.guild)
            
            # DM an Team-Repräsentanten
            requester = team_requester.get(team_name)
            if requester:
                try:
                    if moved_size == event["teams"][team_name]:
                        await requester.send(f"Gute Neuigkeiten! Dein Team {team_name} wurde komplett von der Warteliste in die Anmeldung für das Event '{event['name']}' aufgenommen.")
                    else:
                        await requester.send(f"Gute Neuigkeiten! {moved_size} Spieler deines Teams {team_name} wurden von der Warteliste in die Anmeldung für das Event '{event['name']}' aufgenommen.")
                except discord.errors.Forbidden:
                    logger.warning(f"Could not send DM to {requester}")
                    # Log für fehlgeschlagene DM
                    if interaction and interaction.guild:
                        await send_to_log_channel(
                            f"⚠️ Konnte keine DM an {requester.name} (Team {team_name}) senden", 
                            level="WARNING", 
                            guild=interaction.guild
                        )

async def admin_add_team(interaction, team_name, size, discord_user_id=None, discord_username=None, force_waitlist=False):
    """
    Funktion für Admins, um ein Team hinzuzufügen
    
    Parameters:
    - interaction: Discord-Interaktion
    - team_name: Name des Teams
    - size: Größe des Teams
    - discord_user_id: Optional - Discord-ID des Nutzers, der dem Team zugewiesen wird
    - discord_username: Optional - Username des Nutzers
    - force_waitlist: Ob das Team direkt auf die Warteliste gesetzt werden soll
    
    Returns:
    - True bei Erfolg, False bei Fehler
    """
    # Log-Eintrag für Admin-Aktion
    admin_name = getattr(interaction.user, "name", "Unbekannter Admin")
    await send_to_log_channel(
        f"👤 Admin-Aktion: {admin_name} versucht, Team '{team_name}' mit {size} Mitgliedern hinzuzufügen" + 
        (f" (direkt auf Warteliste)" if force_waitlist else ""),
        guild=interaction.guild
    )
    event = get_event()
    if not event:
        await interaction.response.send_message(
            "Es gibt derzeit kein aktives Event.",
            ephemeral=True
        )
        return False
    
    # Prüfe, ob das Team bereits existiert
    if team_name in event["teams"]:
        await interaction.response.send_message(
            f"Team {team_name} ist bereits angemeldet. Verwende die Team-Bearbeitung, um die Größe zu ändern.",
            ephemeral=True
        )
        return False
    
    # Prüfe, ob Team bereits auf der Warteliste steht
    for wl_team, _ in event["waitlist"]:
        if wl_team == team_name:
            await interaction.response.send_message(
                f"Team {team_name} steht bereits auf der Warteliste. Verwende die Team-Bearbeitung, um die Größe zu ändern.",
                ephemeral=True
            )
            return False
    
    max_team_size = event["max_team_size"]
    
    # Validiere Team-Größe
    if size <= 0 or size > max_team_size:
        await interaction.response.send_message(
            f"Die Teamgröße muss zwischen 1 und {max_team_size} liegen.",
            ephemeral=True
        )
        return False
    
    # Bestimme, ob auf Warteliste oder direktes Hinzufügen
    if force_waitlist:
        # Direkt auf Warteliste setzen
        event["waitlist"].append((team_name, size))
        
        # Setze Benutzer-Team-Zuweisung, wenn angegeben
        if discord_user_id:
            user_team_assignments[discord_user_id] = team_name
            team_requester[team_name] = await bot.fetch_user(int(discord_user_id))
        
        await interaction.response.send_message(
            f"Team {team_name} wurde mit {size} Personen auf die Warteliste gesetzt (Position {len(event['waitlist'])}).",
            ephemeral=True
        )
        
        # Log-Eintrag
        logger.info(f"Admin {interaction.user.name} hat Team {team_name} mit {size} Personen zur Warteliste hinzugefügt.")
        # Log zum Kanal senden
        await send_to_log_channel(
            f"📝 Admin {interaction.user.name} hat Team '{team_name}' mit {size} Personen zur Warteliste hinzugefügt.",
            guild=interaction.guild
        )
    else:
        # Prüfe, ob genügend Slots verfügbar sind
        available_slots = event["max_slots"] - event["slots_used"]
        
        if size <= available_slots:
            # Genügend Plätze verfügbar, direkt anmelden
            event["slots_used"] += size
            event["teams"][team_name] = size
            
            # Setze Benutzer-Team-Zuweisung, wenn angegeben
            if discord_user_id:
                user_team_assignments[discord_user_id] = team_name
            
            await interaction.response.send_message(
                f"Team {team_name} wurde mit {size} Personen angemeldet.",
                ephemeral=True
            )
            
            # Log-Eintrag
            logger.info(f"Admin {interaction.user.name} hat Team {team_name} mit {size} Personen angemeldet.")
            # Log zum Kanal senden
            await send_to_log_channel(
                f"✅ Admin {interaction.user.name} hat Team '{team_name}' mit {size} Personen angemeldet.",
                guild=interaction.guild
            )
        else:
            # Nicht genügend Plätze verfügbar
            if available_slots > 0:
                # Teilweise anmelden und Rest auf Warteliste
                waitlist_size = size - available_slots
                
                # Aktualisiere die angemeldete Teamgröße
                event["slots_used"] += available_slots
                event["teams"][team_name] = available_slots
                
                # Füge Rest zur Warteliste hinzu
                event["waitlist"].append((team_name, waitlist_size))
                
                # Setze Benutzer-Team-Zuweisung, wenn angegeben
                if discord_user_id:
                    user_team_assignments[discord_user_id] = team_name
                    team_requester[team_name] = await bot.fetch_user(int(discord_user_id))
                
                await interaction.response.send_message(
                    f"Team {team_name} wurde teilweise angemeldet. "
                    f"{available_slots} Spieler sind angemeldet und "
                    f"{waitlist_size} Spieler wurden auf die Warteliste gesetzt (Position {len(event['waitlist'])}).",
                    ephemeral=True
                )
                
                # Log-Eintrag
                logger.info(f"Admin {interaction.user.name} hat Team {team_name} teilweise angemeldet: {available_slots} angemeldet, {waitlist_size} auf Warteliste.")
                # Log zum Kanal senden
                await send_to_log_channel(
                    f"⚠️ Admin {interaction.user.name} hat Team '{team_name}' teilweise angemeldet: {available_slots} Mitglieder registriert, {waitlist_size} auf Warteliste.",
                    guild=interaction.guild
                )
            else:
                # Komplett auf Warteliste setzen
                event["waitlist"].append((team_name, size))
                
                # Setze Benutzer-Team-Zuweisung, wenn angegeben
                if discord_user_id:
                    user_team_assignments[discord_user_id] = team_name
                    team_requester[team_name] = await bot.fetch_user(int(discord_user_id))
                
                await interaction.response.send_message(
                    f"Team {team_name} wurde mit {size} Personen auf die Warteliste gesetzt (Position {len(event['waitlist'])}).",
                    ephemeral=True
                )
                
                # Log-Eintrag
                logger.info(f"Admin {interaction.user.name} hat Team {team_name} mit {size} Personen zur Warteliste hinzugefügt (keine Slots verfügbar).")
                # Log zum Kanal senden
                await send_to_log_channel(
                    f"📝 Admin {interaction.user.name} hat Team '{team_name}' mit {size} Personen zur Warteliste hinzugefügt (keine Slots verfügbar).",
                    guild=interaction.guild
                )
    
    # Speichere Änderungen
    save_data(event_data, channel_id, user_team_assignments)
    
    # Benachrichtigung für Discord-Benutzer, wenn angegeben
    if discord_user_id and discord_username:
        try:
            user = await bot.fetch_user(int(discord_user_id))
            if user:
                # Erstelle eine Benachrichtigung
                message = f"Hallo {discord_username}! Ein Admin hat dich dem Team **{team_name}** für das Event '{event['name']}' zugewiesen."
                
                if team_name in event["teams"]:
                    message += f" Das Team ist erfolgreich angemeldet mit {event['teams'][team_name]} Spielern."
                else:
                    # Suche in der Warteliste
                    for wl_team, wl_size in event["waitlist"]:
                        if wl_team == team_name:
                            message += f" Das Team steht auf der Warteliste (Position {event['waitlist'].index((wl_team, wl_size))+1}) mit {wl_size} Spielern."
                            break
                
                await user.send(message)
        except Exception as e:
            logger.warning(f"Konnte Benutzer {discord_user_id} nicht benachrichtigen: {e}")
    
    # Update channel with latest event details
    if channel_id:
        channel = bot.get_channel(interaction.channel_id)
        if channel:
            await send_event_details(channel)
    
    return True

async def send_event_details(channel, event=None):
    """Send event details to a channel with interactive buttons"""
    if event is None:
        event = get_event()
    
    try:
        embed = format_event_details(event)
        
        # Get the user's roles for button states
        has_admin = False
        has_clan_rep = False
        has_team = False
        team_name = None
        
        # Check if the message is for a specific user
        if hasattr(channel, 'author'):
            user = channel.author
            user_id = str(user.id)
            has_admin = has_role(user, ORGANIZER_ROLE)
            has_clan_rep = has_role(user, CLAN_REP_ROLE)
            team_name = user_team_assignments.get(user_id)
            has_team = team_name is not None
        
        # Add interactive buttons
        view = EventActionView(event, has_admin, has_clan_rep, has_team, team_name)
        
        if isinstance(embed, discord.Embed):
            await channel.send(embed=embed, view=view)
        else:
            await channel.send(embed, view=view)
    except Exception as e:
        logger.error(f"Error sending event details: {e}")
        # Fallback to plain text if embed fails
        await channel.send(format_event_list(event))

@bot.event
async def on_ready():
    """Handle bot ready event"""
    logger.info(f"Bot eingeloggt als {bot.user}")
    global channel_id
    
    # Initialisiere Log-Kanal
    from config import LOG_CHANNEL_NAME, LOG_CHANNEL_ID
    
    # Suche nach dem Log-Kanal in allen Guilds
    for guild in bot.guilds:
        log_channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        
        # Wenn kein Log-Kanal gefunden wurde, versuche, einen zu erstellen (falls Berechtigungen vorhanden)
        if not log_channel:
            try:
                # Überprüfe, ob der Bot die erforderlichen Berechtigungen hat
                guild_me = guild.get_member(bot.user.id)
                if guild_me and guild_me.guild_permissions.manage_channels:
                    logger.info(f"Erstelle Log-Kanal '{LOG_CHANNEL_NAME}' in Guild '{guild.name}'")
                    # Erstelle einen neuen Kanal mit eingeschränkten Berechtigungen
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(read_messages=False),
                        guild_me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                    }
                    # Finde die Orga-Rolle und gib ihr Leserechte
                    from config import ORGANIZER_ROLE
                    orga_role = discord.utils.get(guild.roles, name=ORGANIZER_ROLE)
                    if orga_role:
                        overwrites[orga_role] = discord.PermissionOverwrite(read_messages=True, send_messages=False)
                    
                    # Erstelle den Kanal
                    log_channel = await guild.create_text_channel(
                        LOG_CHANNEL_NAME,
                        overwrites=overwrites,
                        topic="Log-Kanal für den Event-Bot. Hier werden wichtige Ereignisse protokolliert."
                    )
                    logger.info(f"Log-Kanal '{LOG_CHANNEL_NAME}' erstellt in Guild '{guild.name}'")
                else:
                    logger.warning(f"Keine Berechtigung zum Erstellen eines Log-Kanals in Guild '{guild.name}'")
            except Exception as e:
                logger.error(f"Fehler beim Erstellen des Log-Kanals in Guild '{guild.name}': {e}")
        
        if log_channel:
            # Wenn gefunden oder erstellt, ID in der Konfiguration speichern
            import config
            config.LOG_CHANNEL_ID = log_channel.id
            logger.info(f"Log-Kanal initialisiert: {log_channel.name} (ID: {log_channel.id})")
            await send_to_log_channel(f"Event-Bot gestartet!", guild=guild)
            
            # Initialisiere globale Log-Kanal-Variable für andere Module
            from utils import discord_log_channel
            import utils
            utils.discord_log_channel = log_channel
            
            break
    
    # Initialisiere Hauptkanal
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            logger.info(f"Channel gefunden: {channel.name}")
            await channel.send("Event-Bot ist online und bereit!")
            await send_to_log_channel(f"Hauptkanal initialisiert: {channel.name} ({channel.id})")
        else:
            logger.warning("Gespeicherter Channel konnte nicht gefunden werden.")
            await send_to_log_channel("Gespeicherter Hauptkanal konnte nicht gefunden werden.", level="WARNING")
    else:
        logger.warning("Kein Channel gesetzt. Bitte nutze den Slash-Befehl /set_channel, um einen Channel zu definieren.")
        await send_to_log_channel("Kein Hauptkanal gesetzt. Bitte /set_channel verwenden.", level="WARNING")

    # Starte die Hintergrund-Tasks
    bot.loop.create_task(check_waitlist_and_expiry())
    bot.loop.create_task(process_log_queue())

async def process_log_queue():
    """Background task to process and send log messages to Discord channel"""
    await bot.wait_until_ready()
    
    # Warte, bis der Bot vollständig bereit ist
    await asyncio.sleep(5)
    
    while not bot.is_closed():
        try:
            # Wenn kein Discord-Kanal verfügbar ist, überspringe
            from utils import discord_log_channel
            if not discord_log_channel:
                await asyncio.sleep(10)
                continue
            
            # Hole Logs aus dem Handler (max. 5 auf einmal)
            logs = discord_handler.get_logs(5)
            
            if not logs:
                # Keine neuen Logs, kurze Pause
                await asyncio.sleep(1)
                continue
            
            # Kombiniere die Logs für eine Nachricht
            combined_message = ""
            
            for level, message in logs:
                # Formatiere die Nachricht je nach Log-Level
                if level == "INFO":
                    formatted_line = f"ℹ️ {message}\n"
                elif level == "WARNING":
                    formatted_line = f"⚠️ {message}\n"
                elif level == "ERROR":
                    formatted_line = f"❌ {message}\n"
                elif level == "CRITICAL":
                    formatted_line = f"🚨 {message}\n"
                else:
                    formatted_line = f"  {message}\n"
                
                combined_message += formatted_line
            
            # Sende die kombinierten Nachrichten
            if combined_message:
                try:
                    # Kürze die Nachricht, wenn sie zu lang ist
                    if len(combined_message) > 1900:
                        combined_message = combined_message[:1900] + "...\n(Nachricht gekürzt)"
                    
                    await discord_log_channel.send(f"```\n{combined_message}\n```")
                except Exception as e:
                    logger.error(f"Fehler beim Senden von Log-Nachrichten an Discord: {e}")
            
            # Kurze Pause, um Discord-Rate-Limits zu respektieren
            await asyncio.sleep(2)
            
        except Exception as e:
            logger.error(f"Fehler in process_log_queue: {e}")
            await asyncio.sleep(10)  # Längere Pause bei Fehlern

async def check_waitlist_and_expiry():
    """Background task to check waitlist and event expiry"""
    await bot.wait_until_ready()
    
    while not bot.is_closed():
        try:
            await asyncio.sleep(WAITLIST_CHECK_INTERVAL)
            event = get_event()

            if not event:
                continue
                
            # Überprüfe, ob expiry_date vorhanden ist
            if "expiry_date" not in event:
                # Wenn nicht, überspringen wir die Verfallsprüfung
                logger.warning("Event hat kein expiry_date, überspringe Verfallsprüfung")
                continue

            # Check for event expiry
            if datetime.now() > event["expiry_date"]:
                logger.info("Event expired, removing it")
                
                event_name = event.get("name", "Unbekanntes Event")
                
                event_data.clear()
                save_data(event_data, channel_id, user_team_assignments)
                
                # Systemlognachricht zum Event-Ablauf
                for guild in bot.guilds:
                    await send_to_log_channel(
                        f"⏰ Event '{event_name}' ist automatisch abgelaufen und wurde aus dem System entfernt.",
                        level="INFO",
                        guild=guild
                    )
                
                if channel_id:
                    channel = bot.get_channel(channel_id)
                    if channel:
                        await channel.send("Das Event ist abgelaufen und wurde gelöscht.")
                continue

            # Check for free slots and process waitlist
            if event["slots_used"] < event["max_slots"] and event["waitlist"]:
                available_slots = event["max_slots"] - event["slots_used"]
                update_needed = False
                
                while available_slots > 0 and event["waitlist"]:
                    team_name, size = event["waitlist"][0]
                    
                    if size <= available_slots:
                        # Remove from waitlist and add to registered teams
                        event["waitlist"].pop(0)
                        event["slots_used"] += size
                        event["teams"][team_name] = event["teams"].get(team_name, 0) + size
                        available_slots -= size
                        update_needed = True
                        
                        # Notify team representative
                        if channel_id:
                            channel = bot.get_channel(channel_id)
                            if channel:
                                await channel.send(f"Team {team_name} wurde von der Warteliste in die Anmeldung aufgenommen!")
                        
                        requester = team_requester.get(team_name)
                        if requester:
                            try:
                                await requester.send(f"Gute Neuigkeiten! Dein Team {team_name} wurde von der Warteliste in die Anmeldung für das Event '{event['name']}' aufgenommen.")
                            except discord.errors.Forbidden:
                                logger.warning(f"Could not send DM to {requester}")
                    else:
                        break

                if update_needed:
                    save_data(event_data, channel_id, user_team_assignments)
                    
                    # Log für automatische Wartelisten-Verarbeitung
                    for guild in bot.guilds:
                        await send_to_log_channel(
                            f"⏫ Automatische Wartelisten-Verarbeitung: Teams wurden automatisch von der Warteliste aufgenommen",
                            level="INFO",
                            guild=guild
                        )
                    
                    if channel_id:
                        channel = bot.get_channel(channel_id)
                        if channel:
                            await send_event_details(channel)
        
        except Exception as e:
            logger.error(f"Error in waitlist check: {e}")

# Channel commands
@bot.tree.command(name="set_channel", description="Setzt den aktuellen Channel für Event-Updates")
async def set_channel(interaction: discord.Interaction):
    """Set the current channel for event updates"""
    # Kommandoausführung loggen
    logger.info(f"Slash-Command: /set_channel ausgeführt von {interaction.user.name} ({interaction.user.id}) in Kanal {interaction.channel.name}")
    
    # Überprüfe Berechtigungen
    if not interaction.user.guild_permissions.manage_channels:
        logger.warning(f"Berechtigungsfehler: {interaction.user.name} ({interaction.user.id}) hat versucht, /set_channel ohne ausreichende Berechtigungen zu verwenden")
        await interaction.response.send_message("Du benötigst 'Kanäle verwalten'-Berechtigungen, um diesen Befehl zu nutzen.", ephemeral=True)
        return
        
    global channel_id
    channel_id = interaction.channel_id
    save_data(event_data, channel_id, user_team_assignments)
    
    # Log für Channel-Setzung
    await send_to_log_channel(
        f"📌 Event-Channel: {interaction.user.name} hat Channel '{interaction.channel.name}' (ID: {channel_id}) als Event-Channel festgelegt",
        guild=interaction.guild
    )
    
    await interaction.response.send_message(f"Dieser Channel ({interaction.channel.name}) wurde erfolgreich für Event-Interaktionen gesetzt.")
    logger.info(f"Channel gesetzt: {interaction.channel.name} (ID: {channel_id})")

# Event commands
@bot.tree.command(name="event", description="Erstellt ein neues Event (nur für Orga-Team)")
async def create_event_command(interaction: discord.Interaction):
    """Create a new event"""
    # Kommandoausführung loggen
    logger.info(f"Slash-Command: /event ausgeführt von {interaction.user.name} ({interaction.user.id}) in Kanal {interaction.channel.name}")
    
    # Überprüfe Rolle
    if not has_role(interaction.user, ORGANIZER_ROLE):
        logger.warning(f"Berechtigungsfehler: {interaction.user.name} ({interaction.user.id}) hat versucht, /event ohne ausreichende Berechtigungen zu verwenden")
        await interaction.response.send_message(
            f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können Events erstellen.",
            ephemeral=True
        )
        return
        
    # Zeige das Modal an
    modal = EventCreationModal()
    await interaction.response.send_modal(modal)

class EventCreationModal(ui.Modal):
    """Modal für die Event-Erstellung"""
    def __init__(self):
        super().__init__(title="Event erstellen")
        
        # Aktuelles Datum für Platzhalter
        from datetime import datetime
        today = datetime.now().strftime("%d.%m.%Y")
        
        # Felder für Event-Details
        self.event_name = ui.TextInput(
            label="Event-Name",
            placeholder="Name des Events",
            default="CoC",
            required=True,
            min_length=2,
            max_length=50
        )
        self.add_item(self.event_name)
        
        self.event_date = ui.TextInput(
            label="Datum",
            placeholder="TT.MM.JJJJ",
            default=today,
            required=True
        )
        self.add_item(self.event_date)
        
        self.event_time = ui.TextInput(
            label="Uhrzeit",
            placeholder="HH:MM",
            default="20:00",
            required=True
        )
        self.add_item(self.event_time)
        
        self.event_description = ui.TextInput(
            label="Beschreibung",
            placeholder="Details zum Event",
            required=True,
            style=discord.TextStyle.paragraph
        )
        self.add_item(self.event_description)
    
    async def on_submit(self, interaction: discord.Interaction):
        # Extrahiere die Werte aus dem Modal
        name = self.event_name.value.strip()
        date = self.event_date.value.strip()
        time = self.event_time.value.strip()
        description = self.event_description.value.strip()
        
        # Übergebe die Werte an die Event-Erstellungsfunktion
        await create_event_internal(interaction, name, date, time, description)



async def create_event_internal(interaction: discord.Interaction, name: str, date: str, time: str, description: str):
    """Internal function to handle event creation after modal submission"""
    # Kommandoausführung loggen
    logger.info(f"Event-Erstellung: {interaction.user.name} ({interaction.user.id}) erstellt Event mit Parametern: name='{name}', date='{date}', time='{time}'")

    if get_event():
        await interaction.response.send_message("Es existiert bereits ein aktives Event. Bitte lösche es zuerst mit /delete_event.")
        return
    
    # Validate date format
    event_date = parse_date(date)
    if not event_date:
        await interaction.response.send_message("Ungültiges Datumsformat. Bitte verwende das Format TT.MM.JJJJ.")
        return
    
    # Create event
    event_data["event"] = {
        "name": name,
        "date": date,
        "time": time,
        "description": description,
        "teams": {},
        "waitlist": [],
        "max_slots": DEFAULT_MAX_SLOTS,
        "slots_used": 0,
        "max_team_size": DEFAULT_MAX_TEAM_SIZE,
        "expiry_date": event_date + timedelta(days=1)
    }

    save_data(event_data, channel_id, user_team_assignments)
    await interaction.response.send_message("Event erfolgreich erstellt!")
    
    # Log zum Erstellen des Events
    await send_to_log_channel(
        f"🆕 Event erstellt: '{name}' am {date} um {time} durch {interaction.user.name}",
        guild=interaction.guild
    )
    
    # Get channel after creating the event
    channel = bot.get_channel(interaction.channel_id)
    if channel:
        # Check roles for this specific user
        user_id = str(interaction.user.id)
        has_admin = has_role(interaction.user, ORGANIZER_ROLE)
        has_clan_rep = has_role(interaction.user, CLAN_REP_ROLE)
        team_name = user_team_assignments.get(user_id)
        has_team = team_name is not None
        
        # Create embed
        embed = format_event_details(get_event())
        view = EventActionView(get_event(), has_admin, has_clan_rep, has_team, team_name)
        
        await channel.send(embed=embed, view=view)

@bot.tree.command(name="delete_event", description="Löscht das aktuelle Event (nur für Orga-Team)")
async def delete_event(interaction: discord.Interaction):
    """Delete the current event"""
    # Kommandoausführung loggen
    logger.info(f"Slash-Command: /delete_event ausgeführt von {interaction.user.name} ({interaction.user.id}) in Kanal {interaction.channel.name}")
    
    # Überprüfe Rolle
    if not has_role(interaction.user, ORGANIZER_ROLE):
        logger.warning(f"Berechtigungsfehler: {interaction.user.name} ({interaction.user.id}) hat versucht, /delete_event ohne ausreichende Berechtigungen zu verwenden")
        await send_feedback(interaction,
            f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können Events löschen.", 
            ephemeral=True
        )
        return

    event = get_event()
    if not event:
        await send_feedback(interaction, "Es gibt kein aktives Event zum Löschen.", ephemeral=True)
        return
    
    # Zeige eine Bestätigungsanfrage mit den Konsequenzen des Löschens
    embed = discord.Embed(
        title="⚠️ Event wirklich löschen?",
        description=f"Bist du sicher, dass du das Event **{event['name']}** löschen möchtest?\n\n"
                    f"Diese Aktion kann nicht rückgängig gemacht werden! Alle Team-Anmeldungen und Wartelisten-Einträge werden gelöscht.",
        color=discord.Color.red()
    )
    
    # Details zum Event hinzufügen
    embed.add_field(
        name="Event-Details", 
        value=f"**Name:** {event['name']}\n"
              f"**Datum:** {event.get('date', 'Nicht angegeben')}\n"
              f"**Angemeldete Teams:** {len(event['teams'])}\n"
              f"**Teams auf Warteliste:** {len(event['waitlist'])}"
    )
    
    # Verwende die vorhandene Bestätigungsansicht
    view = DeleteConfirmationView()
    await send_feedback(interaction, "", embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="show_event", description="Zeigt das aktuelle Event an")
async def show_event(interaction: discord.Interaction):
    """Show the current event"""
    event = get_event()
    if not event:
        await interaction.response.send_message("Es gibt derzeit kein aktives Event.", ephemeral=True)
        return
    
    # Prüfe, ob es ein echtes Event mit Inhalt ist
    if not event.get('name') or not event.get('date'):
        await interaction.response.send_message("Es gibt derzeit kein aktives Event.", ephemeral=True)
        return
    
    # Es gibt ein Event, zeige die Details mit Buttons
    await interaction.response.send_message("Hier sind die Event-Details:")
    
    # Get channel after sending initial response
    channel = bot.get_channel(interaction.channel_id)
    if channel:
        # Check roles for this specific user
        user_id = str(interaction.user.id)
        has_admin = has_role(interaction.user, ORGANIZER_ROLE)
        has_clan_rep = has_role(interaction.user, CLAN_REP_ROLE)
        team_name = user_team_assignments.get(user_id)
        has_team = team_name is not None
        
        # Create embed
        embed = format_event_details(event)
        view = EventActionView(event, has_admin, has_clan_rep, has_team, team_name)
        
        await channel.send(embed=embed, view=view)

# Registration commands
@bot.tree.command(name="reg", description="Meldet dein Team an oder ändert die Teamgröße (nur für Clan-Rep)")
@app_commands.describe(
    team_name="Name des Teams", 
    size="Anzahl der Teilnehmer (0 zum Entfernen des Teams)"
)
async def register_team(interaction: discord.Interaction, team_name: str, size: int):
    """Register a team or update team size. Size 0 unregisters the team."""
    # Validiere den Befehlskontext (Rolle, Event)
    event, _ = await validate_command_context(interaction, required_role=CLAN_REP_ROLE)
    if not event:
        return

    # Normalisiere den Team-Namen
    team_name = team_name.strip()
    user_id = str(interaction.user.id)

    # Validiere die Teamgröße
    if not await validate_team_size(interaction, size, event["max_team_size"]):
        return

    # Prüfe, ob der Nutzer bereits einem anderen Team zugewiesen ist
    if user_id in user_team_assignments and user_team_assignments[user_id].lower() != team_name.lower():
        assigned_team = user_team_assignments[user_id]
        await send_feedback(
            interaction,
            f"Du bist bereits dem Team '{assigned_team}' zugewiesen. Du kannst nur für ein Team anmelden.",
            ephemeral=True
        )
        return

    # Team-Details abrufen (Event + Warteliste)
    event_size, waitlist_size, total_size, registered_name, waitlist_entries = get_team_total_size(event, team_name)
    
    # Abmeldung (size == 0)
    if size == 0:
        await handle_team_unregistration(interaction, team_name)
        return
    
    # Nutzer für Benachrichtigungen speichern
    team_requester[team_name] = interaction.user
    
    # Verwende update_team_size für die eigentliche Logik
    success = await update_team_size(interaction, team_name, size)
    
    if success:
        # Speichere Daten nach jeder Änderung
        save_data(event_data, channel_id, user_team_assignments)
        
        # Aktualisiere die Event-Anzeige
        await update_event_displays(interaction=interaction)

# Der /wl-Befehl wurde entfernt, da die Warteliste jetzt automatisch vom Bot verwaltet wird

@bot.tree.command(name="open_reg", description="Erhöht die maximale Teamgröße oder entfernt die Begrenzung (nur für Orga-Team)")
async def open_registration(interaction: discord.Interaction):
    """Increases maximum team size or removes the limit (admin only)"""
    # Überprüfe Rolle
    if not has_role(interaction.user, ORGANIZER_ROLE):
        await send_feedback(interaction,
            f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können die Registrierung öffnen.",
            ephemeral=True
        )
        return

    event = get_event()
    if not event:
        await send_feedback(interaction, "Es gibt derzeit kein aktives Event.")
        return
    
    current_max_size = event["max_team_size"]
    new_max_size = None
    message = ""
    
    # Logik für verschiedene Fälle:
    # Fall 1: Max. Teamgröße ist 9 -> auf 18 erhöhen
    # Fall 2: Max. Teamgröße ist 18 -> Begrenzung aufheben (99)
    # Fall 3: Keine Begrenzung mehr -> Nichts tun
    
    if current_max_size == DEFAULT_MAX_TEAM_SIZE:
        # Fall 1: Von 9 auf 18 erhöhen
        new_max_size = EXPANDED_MAX_TEAM_SIZE
        message = f"Die maximale Teamgröße wurde auf {new_max_size} erhöht."
    elif current_max_size == EXPANDED_MAX_TEAM_SIZE:
        # Fall 2: Begrenzung aufheben (auf 99 setzen)
        new_max_size = 99  # Praktisch unbegrenzt
        message = f"Die Begrenzung der Teamgröße wurde aufgehoben. Teams können jetzt beliebig groß sein."
    else:
        # Fall 3: Keine Änderung notwendig
        await send_feedback(interaction, "Die Teamgröße ist bereits unbegrenzt.")
        return
    
    # Speichere die alte Teamgröße für das Logging
    old_max_size = event["max_team_size"]
    
    # Aktualisiere die maximale Teamgröße
    event["max_team_size"] = new_max_size
    save_data(event_data, channel_id, user_team_assignments)
    
    # Log für die Änderung der maximalen Teamgröße
    log_message = f"⬆️ Teamgröße angepasst: Admin {interaction.user.name} hat die maximale Teamgröße für Event '{event['name']}' von {old_max_size} auf {new_max_size} geändert"
    await send_to_log_channel(log_message, guild=interaction.guild)
    
    # Benutzer-Feedback
    await send_feedback(interaction, message)
    
    # Ankündigung im Event-Kanal
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            channel_message = f"📢 **Ankündigung**: Die maximale Teamgröße für das Event '{event['name']}' wurde angepasst! {message}"
            await channel.send(channel_message)
            await send_event_details(channel)

@bot.tree.command(name="reset_team_assignment", description="Setzt die Team-Zuweisung eines Nutzers zurück (nur für Orga-Team)")
@app_commands.describe(
    user="Der Nutzer, dessen Team-Zuweisung zurückgesetzt werden soll"
)
async def reset_team_assignment(interaction: discord.Interaction, user: discord.User):
    """Reset a user's team assignment (admin only)"""
    # Überprüfe Rolle
    if not has_role(interaction.user, ORGANIZER_ROLE):
        await interaction.response.send_message(
            f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können diese Aktion ausführen.",
            ephemeral=True
        )
        return

    user_id = str(user.id)
    
    if user_id not in user_team_assignments:
        await interaction.response.send_message(f"{user.display_name} ist keinem Team zugewiesen.")
        return
    
    team_name = user_team_assignments[user_id]
    del user_team_assignments[user_id]
    save_data(event_data, channel_id, user_team_assignments)
    
    # Log für Zurücksetzen der Team-Zuweisung
    await send_to_log_channel(
        f"🔄 Team-Zuweisung zurückgesetzt: Admin {interaction.user.name} hat die Zuweisung von {user.display_name} zum Team '{team_name}' entfernt",
        guild=interaction.guild
    )
    
    await interaction.response.send_message(
        f"Team-Zuweisung für {user.display_name} (Team {team_name}) wurde zurückgesetzt."
    )
    
    # Try to notify the user
    try:
        await user.send(
            f"Deine Team-Zuweisung (Team {team_name}) wurde von einem Administrator zurückgesetzt. "
            f"Du kannst dich nun einem anderen Team anschließen."
        )
    except discord.errors.Forbidden:
        # User has DMs disabled, continue silently
        pass

# Team List and CSV Export Commands
@bot.tree.command(name="team_list", description="Zeigt eine schön formatierte Liste aller angemeldeten Teams")
async def team_list(interaction: discord.Interaction):
    """Display a formatted list of all registered teams"""
    event = get_event()
    if not event:
        await interaction.response.send_message("Es gibt derzeit kein aktives Event.")
        return
    
    # Create formatted team list embed
    embed = discord.Embed(
        title=f"Teamliste für {event['name']}",
        description=f"Datum: {event['date']} | Uhrzeit: {event['time']}",
        color=discord.Color.blue()
    )
    
    # Add registered teams section
    if event["teams"]:
        registered_text = ""
        # Prüfe, ob das Team-Dictionary jetzt das erweiterte Format mit IDs verwendet
        using_team_ids = False
        if event["teams"] and isinstance(next(iter(event["teams"].values())), dict):
            using_team_ids = True
        
        if using_team_ids:
            # Neues Format mit Team-IDs
            for idx, (team_name, data) in enumerate(sorted(event["teams"].items()), 1):
                team_size = data.get("size", 0)
                team_id = data.get("id", "")
                registered_text += f"**{idx}.** {team_name.capitalize()} - {team_size} Mitglieder | ID: `{team_id}`\n"
        else:
            # Altes Format ohne Team-IDs
            for idx, (team_name, size) in enumerate(sorted(event["teams"].items()), 1):
                registered_text += f"**{idx}.** {team_name.capitalize()} - {size} Mitglieder\n"
        
        embed.add_field(
            name=f"📋 Angemeldete Teams ({event['slots_used']}/{event['max_slots']} Slots)",
            value=registered_text,
            inline=False
        )
    else:
        embed.add_field(
            name=f"📋 Angemeldete Teams (0/{event['max_slots']} Slots)",
            value="Noch keine Teams angemeldet.",
            inline=False
        )
    
    # Add waitlist section
    if event["waitlist"]:
        waitlist_text = ""
        # Prüfe, ob die Warteliste das erweiterte Format mit IDs verwendet
        using_waitlist_ids = False
        if event["waitlist"] and len(event["waitlist"][0]) > 2:
            using_waitlist_ids = True
        
        if using_waitlist_ids:
            # Neues Format mit Team-IDs
            for idx, entry in enumerate(event["waitlist"], 1):
                if len(entry) >= 3:  # Format: (team_name, size, team_id)
                    team_name, size, team_id = entry
                    waitlist_text += f"**{idx}.** {team_name.capitalize()} - {size} Mitglieder | ID: `{team_id}`\n"
        else:
            # Altes Format ohne Team-IDs
            for idx, (team_name, size) in enumerate(event["waitlist"], 1):
                waitlist_text += f"**{idx}.** {team_name.capitalize()} - {size} Mitglieder\n"
        
        embed.add_field(
            name="⏳ Warteliste",
            value=waitlist_text,
            inline=False
        )
    else:
        embed.add_field(
            name="⏳ Warteliste",
            value="Keine Teams auf der Warteliste.",
            inline=False
        )
    
    # Add statistics
    available_slots = event["max_slots"] - event["slots_used"]
    embed.add_field(
        name="📊 Statistik",
        value=f"Anzahl Teams: **{len(event['teams'])}**\n"
              f"Verfügbare Slots: **{available_slots}**\n"
              f"Warteliste: **{len(event['waitlist'])}** Teams\n"
              f"Max. Teamgröße: **{event['max_team_size']}**",
        inline=False
    )
    
    embed.set_footer(text=f"Erstellt am {datetime.now().strftime('%d.%m.%Y um %H:%M')} Uhr")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="export_csv", description="Exportiert die Teamliste als CSV-Datei (nur für Orga-Team)")
async def export_csv(interaction: discord.Interaction):
    """Export team data as CSV file"""
    # Überprüfe Berechtigung
    if not has_role(interaction.user, ORGANIZER_ROLE):
        await interaction.response.send_message(
            f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können Team-Daten exportieren.",
            ephemeral=True
        )
        return
    
    event = get_event()
    if not event:
        await interaction.response.send_message("Es gibt derzeit kein aktives Event.")
        return
    
    # Create CSV in memory
    output = io.StringIO()
    csv_writer = csv.writer(output)
    
    # Write header
    csv_writer.writerow(["Team", "Größe", "Status", "Team-ID"])
    
    # Write registered teams
    # Prüfe, ob das Team-Dictionary das erweiterte Format mit IDs verwendet
    using_team_ids = False
    if event["teams"] and isinstance(next(iter(event["teams"].values())), dict):
        using_team_ids = True
    
    if using_team_ids:
        # Neues Format mit Team-IDs
        for team_name, data in event["teams"].items():
            size = data.get("size", 0)
            team_id = data.get("id", "")
            csv_writer.writerow([team_name, size, "Angemeldet", team_id])
    else:
        # Altes Format ohne Team-IDs
        for team_name, size in event["teams"].items():
            csv_writer.writerow([team_name, size, "Angemeldet", ""])
    
    # Write waitlist teams
    # Prüfe, ob die Warteliste das erweiterte Format mit IDs verwendet
    using_waitlist_ids = False
    if event["waitlist"] and len(event["waitlist"][0]) > 2:
        using_waitlist_ids = True
    
    if using_waitlist_ids:
        # Neues Format mit Team-IDs
        for entry in event["waitlist"]:
            if len(entry) >= 3:  # Format: (team_name, size, team_id)
                team_name, size, team_id = entry
                csv_writer.writerow([team_name, size, "Warteliste", team_id])
    else:
        # Altes Format ohne Team-IDs
        for team_name, size in event["waitlist"]:
            csv_writer.writerow([team_name, size, "Warteliste", ""])
    
    # Reset stream position to start
    output.seek(0)
    
    # Create discord file object
    event_date = event["date"].replace(".", "-")
    filename = f"teams_{event_date}.csv"
    file = discord.File(fp=io.BytesIO(output.getvalue().encode('utf-8')), filename=filename)
    
    await interaction.response.send_message(f"Hier ist die exportierte Teamliste für {event['name']}:", file=file)

@bot.tree.command(name="help", description="Zeigt Hilfe zu den verfügbaren Befehlen")
async def help_command(interaction: discord.Interaction):
    """Show help information"""
    # Kommandoausführung loggen
    logger.info(f"Slash-Command: /help ausgeführt von {interaction.user.name} ({interaction.user.id}) in Kanal {interaction.channel.name}")
    
    # Create help embed
    embed = discord.Embed(
        title="📚 Event-Bot Hilfe",
        description="Hier sind die verfügbaren Befehle:",
        color=discord.Color.blue()
    )
    
    # Get user roles
    is_admin = has_role(interaction.user, ORGANIZER_ROLE)
    is_clan_rep = has_role(interaction.user, CLAN_REP_ROLE)
    
    # Basic commands for everyone
    embed.add_field(
        name="🔍 Allgemeine Befehle",
        value=(
            "• `/help` - Zeigt diese Hilfe an\n"
            "• `/show_event` - Zeigt das aktuelle Event an\n"
        ),
        inline=False
    )
    
    # Commands for clan reps
    if is_clan_rep:
        embed.add_field(
            name="👥 Team-Verwaltung (für Clan-Rep)",
            value=(
                f"• `/reg [team_name] [size]` - Meldet dein Team an oder ändert die Teamgröße\n"
                f"Die Warteliste wird automatisch vom Bot verwaltet, wenn nicht genügend Slots verfügbar sind.\n"
            ),
            inline=False
        )
    
    # Commands for admins
    if is_admin:
        embed.add_field(
            name="⚙️ Admin-Befehle (nur für Orga-Team)",
            value=(
                "• `/set_channel` - Setzt den aktuellen Channel für Event-Updates\n"
                "• `/event [name] [date] [time] [description]` - Erstellt ein neues Event\n"
                "• `/delete_event` - Löscht das aktuelle Event\n"
                "• `/open_reg` - Erhöht die maximale Teamgröße\n"
                "• `/reset_team_assignment [user]` - Setzt die Team-Zuweisung eines Nutzers zurück\n"
                "• `/close` - Schließt die Anmeldungen für das Event\n"
                "• `/open` - Öffnet die Anmeldungen für das Event wieder\n"
                "• Admin-Menü: Teams verwalten, bearbeiten und hinzufügen\n"
            ),
            inline=False
        )
    
    await send_feedback(interaction, "", embed=embed, ephemeral=True)



@bot.tree.command(name="unregister", description="Meldet dein Team vom Event ab")
async def unregister_command(interaction: discord.Interaction, team_name: str = None):
    """Melde dein Team vom Event ab"""
    # Validiere den Befehlskontext (Event)
    event, _ = await validate_command_context(interaction)
    if not event:
        return
    
    # Definiere user_id aus der Interaktion
    user_id = str(interaction.user.id)
    
    # Wenn kein Team-Name angegeben wurde, versuche das zugeordnete Team zu finden
    if not team_name:
        if user_id in user_team_assignments:
            team_name = user_team_assignments[user_id]
        else:
            await send_feedback(
                interaction,
                "Du bist keinem Team zugeordnet und hast keinen Team-Namen angegeben.",
                ephemeral=True
            )
            return
    
    # Prüfe Berechtigungen
    is_admin = has_role(interaction.user, ORGANIZER_ROLE)
    is_assigned_to_team = (user_id in user_team_assignments and user_team_assignments[user_id].lower() == team_name.lower())
    
    if not is_admin and not is_assigned_to_team:
        await send_feedback(
            interaction,
            f"Du kannst nur dein eigenes Team abmelden, oder benötigst die '{ORGANIZER_ROLE}' Rolle.",
            ephemeral=True
        )
        return
    
    # Verwende handle_team_unregistration für die eigentliche Abmeldungslogik
    await handle_team_unregistration(interaction, team_name, is_admin)

@bot.tree.command(name="update", description="Aktualisiert die Details des aktuellen Events")
async def update_command(interaction: discord.Interaction):
    """Aktualisiert die Event-Details im Kanal"""
    # Validiere den Befehlskontext (Rolle, Event)
    event, _ = await validate_command_context(interaction, required_role=ORGANIZER_ROLE)
    if not event:
        return
    
    # Prüfe, ob ein Kanal gesetzt wurde
    if not channel_id:
        await send_feedback(
            interaction,
            "Es wurde noch kein Kanal gesetzt. Bitte verwende /set_channel, um einen Kanal festzulegen.",
            ephemeral=True
        )
        return
    
    channel = bot.get_channel(channel_id)
    if not channel:
        await send_feedback(
            interaction,
            "Der gespeicherte Kanal konnte nicht gefunden werden. Bitte setze den Kanal neu mit /set_channel.",
            ephemeral=True
        )
        return
    
    # Aktualisiere die Event-Details im Kanal
    success = await update_event_displays(interaction=interaction, channel=channel)
    
    if success:
        await send_feedback(
            interaction,
            "Die Event-Details wurden im Kanal aktualisiert.",
            ephemeral=True
        )
    else:
        await send_feedback(
            interaction,
            "Es ist ein Fehler beim Aktualisieren der Event-Details aufgetreten.",
            ephemeral=True
        )

@bot.tree.command(name="edit", description="Bearbeitet die Größe deines Teams")
async def edit_command(interaction: discord.Interaction):
    """Bearbeite die Größe deines Teams"""
    # Validiere den Befehlskontext (Event, Team-Zugehörigkeit)
    event, team_name = await validate_command_context(interaction, team_required=True)
    if not event:
        return
    
    # Hole die Team-Details (Event + Warteliste)
    event_size, waitlist_size, total_size, registered_name, waitlist_entries = get_team_total_size(event, team_name)
    
    if total_size == 0:
        await send_feedback(
            interaction,
            f"Team '{team_name}' ist weder angemeldet noch auf der Warteliste.",
            ephemeral=True
        )
        return
    
    # Erstelle ein Modal zum Bearbeiten der Teamgröße
    # Verwende registered_name, wenn verfügbar (für korrekte Schreibweise)
    display_name = registered_name if registered_name else team_name
    
    # Prüfe Admin-Status für erweiterte Optionen
    is_admin = has_role(interaction.user, ORGANIZER_ROLE)
    
    modal = TeamEditModal(display_name, total_size, event["max_team_size"], is_admin=is_admin)
    await interaction.response.send_modal(modal)

@bot.tree.command(name="close", description="Schließt die Anmeldungen für das aktuelle Event (nur für Orga-Team)")
async def close_command(interaction: discord.Interaction):
    """Schließt die Anmeldungen für das Event"""
    # Kommandoausführung loggen
    logger.info(f"Slash-Command: /close ausgeführt von {interaction.user.name} ({interaction.user.id}) in Kanal {interaction.channel.name}")
    
    # Validiere den Befehlskontext (Rolle, Event)
    event, _ = await validate_command_context(interaction, required_role=ORGANIZER_ROLE)
    if not event:
        return
    
    # Setze die verfügbaren Slots auf die aktuell verwendeten Slots
    event["max_slots"] = event["slots_used"]
    
    # Speichere die Änderungen
    save_data(event_data, channel_id, user_team_assignments)
    
    await send_feedback(
        interaction,
        f"Die Anmeldungen für das Event '{event['name']}' wurden geschlossen. Neue Teams können nur noch auf die Warteliste.",
        ephemeral=True
    )
    
    # Log eintragen
    await send_to_log_channel(
        f"🔒 Event geschlossen: {interaction.user.name} hat die Anmeldungen für das Event '{event['name']}' geschlossen",
        level="INFO",
        guild=interaction.guild
    )
    
    # Aktualisiere die Event-Details im Kanal
    await update_event_displays(interaction=interaction)

@bot.tree.command(name="open", description="Öffnet die Anmeldungen für das aktuelle Event wieder (nur für Orga-Team)")
async def open_command(interaction: discord.Interaction):
    """Öffnet die Anmeldungen für das Event wieder"""
    # Kommandoausführung loggen
    logger.info(f"Slash-Command: /open ausgeführt von {interaction.user.name} ({interaction.user.id}) in Kanal {interaction.channel.name}")
    
    # Validiere den Befehlskontext (Rolle, Event)
    event, _ = await validate_command_context(interaction, required_role=ORGANIZER_ROLE)
    if not event:
        return
    
    # Speichere die alten Werte für das Log
    old_max_slots = event["max_slots"]
    
    # Setze die verfügbaren Slots auf den Standardwert
    event["max_slots"] = DEFAULT_MAX_SLOTS
    
    # Speichere die Änderungen
    save_data(event_data, channel_id, user_team_assignments)
    
    # Berechne wie viele Slots wieder verfügbar sind
    new_available_slots = DEFAULT_MAX_SLOTS - event["slots_used"]
    
    await send_feedback(
        interaction,
        f"Die Anmeldungen für das Event '{event['name']}' wurden wieder geöffnet. "
        f"Es sind jetzt {new_available_slots} Slots verfügbar.",
        ephemeral=True
    )
    
    # Log eintragen
    await send_to_log_channel(
        f"🔓 Event geöffnet: {interaction.user.name} hat die Anmeldungen für das Event '{event['name']}' wieder geöffnet "
        f"(Slots: {old_max_slots} → {DEFAULT_MAX_SLOTS})",
        level="INFO",
        guild=interaction.guild
    )
    
    # Verarbeite die Warteliste, wenn Slots frei geworden sind
    if new_available_slots > 0:
        await process_waitlist_after_change(interaction, new_available_slots)
    
    # Aktualisiere die Event-Details im Kanal
    await update_event_displays(interaction=interaction)

@bot.tree.command(name="find", description="Findet ein Team oder einen Spieler im Event")
async def find_command(interaction: discord.Interaction, search_term: str):
    """Findet ein Team oder einen Spieler im Event"""
    # Validiere den Befehlskontext (Event)
    event, _ = await validate_command_context(interaction)
    if not event:
        return
    
    search_term = search_term.lower()
    results = []
    
    # Prüfe, ob das Team-Dictionary das erweiterte Format mit IDs verwendet
    using_team_ids = False
    if event["teams"] and isinstance(next(iter(event["teams"].values())), dict):
        using_team_ids = True
    
    # Suche in registrierten Teams
    if using_team_ids:
        # Neues Format mit Team-IDs
        for team_name, data in event["teams"].items():
            if search_term in team_name.lower():
                size = data.get("size", 0)
                team_id = data.get("id", "keine ID")
                results.append(f"✅ **{team_name}**: {size} {'Person' if size == 1 else 'Personen'} (Angemeldet, ID: {team_id})")
    else:
        # Altes Format
        for team_name, size in event["teams"].items():
            if search_term in team_name.lower():
                results.append(f"✅ **{team_name}**: {size} {'Person' if size == 1 else 'Personen'} (Angemeldet)")
    
    # Prüfe, ob die Warteliste das erweiterte Format mit IDs verwendet
    using_waitlist_ids = False
    if event["waitlist"] and len(event["waitlist"][0]) > 2:
        using_waitlist_ids = True
    
    # Suche in Warteliste
    if using_waitlist_ids:
        # Neues Format mit Team-IDs
        for i, entry in enumerate(event["waitlist"]):
            if len(entry) >= 3:  # Format: (team_name, size, team_id)
                team_name, size, team_id = entry
                if search_term in team_name.lower():
                    results.append(f"⏳ **{team_name}**: {size} {'Person' if size == 1 else 'Personen'} (Warteliste Position {i+1}, ID: {team_id})")
    else:
        # Altes Format
        for i, (team_name, size) in enumerate(event["waitlist"]):
            if search_term in team_name.lower():
                results.append(f"⏳ **{team_name}**: {size} {'Person' if size == 1 else 'Personen'} (Warteliste Position {i+1})")
    
    # Suche nach zugewiesenen Benutzern (Discord-ID -> Team)
    user_results = []
    for user_id, team_name in user_team_assignments.items():
        # Versuche, den Benutzer zu finden
        try:
            user = await bot.fetch_user(int(user_id))
            if search_term in user.name.lower() or search_term in str(user.id):
                # Hole die Team-Details
                event_size, waitlist_size, total_size, registered_name, _ = get_team_total_size(event, team_name)
                
                if event_size > 0:
                    user_results.append(f"👤 **{user.name}** (ID: {user.id}) ist in Team **{team_name}** (Angemeldet, Größe: {total_size})")
                elif waitlist_size > 0:
                    # Finde Position auf der Warteliste
                    waitlist_position = "unbekannt"
                    for i, entry in enumerate(event["waitlist"]):
                        if using_waitlist_ids:
                            if len(entry) >= 3 and entry[0].lower() == team_name.lower():
                                waitlist_position = i + 1
                                break
                        else:
                            if entry[0].lower() == team_name.lower():
                                waitlist_position = i + 1
                                break
                    
                    user_results.append(f"👤 **{user.name}** (ID: {user.id}) ist in Team **{team_name}** (Warteliste Position {waitlist_position}, Größe: {total_size})")
        except Exception as e:
            # Bei Fehler einfach überspringen
            logger.error(f"Fehler beim Suchen des Benutzers {user_id}: {e}")
            pass
    
    # Kombiniere die Ergebnisse
    results.extend(user_results)
    
    if results:
        # Erstelle eine Nachricht mit allen Ergebnissen
        message = f"**🔍 Suchergebnisse für '{search_term}':**\n\n" + "\n".join(results)
        
        # Wenn die Nachricht zu lang ist, kürze sie
        if len(message) > 1900:
            message = message[:1900] + "...\n(Weitere Ergebnisse wurden abgeschnitten)"
        
        await send_feedback(interaction, message, ephemeral=True)
    else:
        await send_feedback(
            interaction,
            f"Keine Ergebnisse für '{search_term}' gefunden.",
            ephemeral=True
        )



@bot.tree.command(name="export_teams", description="Exportiert die Teamliste als CSV-Datei (nur für Orga-Team)")
async def export_teams(interaction: discord.Interaction):
    """Exportiert alle Teams als CSV-Datei"""
    # Validiere den Befehlskontext (Rolle, Event)
    event, _ = await validate_command_context(interaction, required_role=ORGANIZER_ROLE)
    if not event:
        return
        
    # Erstelle CSV-Inhalt im Speicher
    import io
    import csv
    from datetime import datetime
    
    csv_file = io.StringIO()
    csv_writer = csv.writer(csv_file)
    
    # Prüfe, ob das Team-Dictionary das erweiterte Format mit IDs verwendet
    using_team_ids = False
    if event["teams"] and isinstance(next(iter(event["teams"].values())), dict):
        using_team_ids = True
    
    # Prüfe, ob die Warteliste das erweiterte Format mit IDs verwendet
    using_waitlist_ids = False
    if event["waitlist"] and len(event["waitlist"][0]) > 2:
        using_waitlist_ids = True
    
    # Erweiterten Header für das neue Format
    if using_team_ids or using_waitlist_ids:
        csv_writer.writerow(["Typ", "Teamname", "Größe", "Teamleiter-Discord-ID", "Team-ID", "Registrierungsdatum"])
    else:
        # Standard-Header für das alte Format
        csv_writer.writerow(["Typ", "Teamname", "Größe", "Teamleiter-Discord-ID", "Registrierungsdatum"])
    
    # Schreibe angemeldete Teams
    if using_team_ids:
        # Neues Format mit Team-IDs
        for team_name, data in event["teams"].items():
            size = data.get("size", 0)
            team_id = data.get("id", "keine ID")
            
            # Finde Team-Leiter (suche ersten Nutzer mit diesem Team)
            leader_id = "Unbekannt"
            for user_id, assigned_team in user_team_assignments.items():
                if assigned_team.lower() == team_name.lower():
                    leader_id = user_id
                    break
            
            if using_team_ids or using_waitlist_ids:
                csv_writer.writerow(["Angemeldet", team_name, size, leader_id, team_id, ""])
            else:
                csv_writer.writerow(["Angemeldet", team_name, size, leader_id, ""])
    else:
        # Altes Format
        for team_name, size in event["teams"].items():
            # Finde Team-Leiter (suche ersten Nutzer mit diesem Team)
            leader_id = "Unbekannt"
            for user_id, assigned_team in user_team_assignments.items():
                if assigned_team.lower() == team_name.lower():
                    leader_id = user_id
                    break
            
            csv_writer.writerow(["Angemeldet", team_name, size, leader_id, ""])
    
    # Schreibe Warteliste
    if using_waitlist_ids:
        # Neues Format mit Team-IDs
        for i, entry in enumerate(event["waitlist"]):
            if len(entry) >= 3:  # Format: (team_name, size, team_id)
                team_name, size, team_id = entry
                
                # Finde Team-Leiter (suche ersten Nutzer mit diesem Team)
                leader_id = "Unbekannt"
                for user_id, assigned_team in user_team_assignments.items():
                    if assigned_team.lower() == team_name.lower():
                        leader_id = user_id
                        break
                
                if using_team_ids or using_waitlist_ids:
                    csv_writer.writerow(["Warteliste", team_name, size, leader_id, team_id, ""])
                else:
                    csv_writer.writerow(["Warteliste", team_name, size, leader_id, ""])
    else:
        # Altes Format
        for i, (team_name, size) in enumerate(event["waitlist"]):
            # Finde Team-Leiter (suche ersten Nutzer mit diesem Team)
            leader_id = "Unbekannt"
            for user_id, assigned_team in user_team_assignments.items():
                if assigned_team.lower() == team_name.lower():
                    leader_id = user_id
                    break
            
            csv_writer.writerow(["Warteliste", team_name, size, leader_id, ""])
    
    # Zurück zum Anfang der Datei
    csv_file.seek(0)
    
    # Aktuelle Zeit für den Dateinamen
    current_time = datetime.now().strftime("%Y-%m-%d_%H-%M")
    filename = f"teamliste_{current_time}.csv"
    
    # Log für CSV-Export
    await send_to_log_channel(
        f"📊 CSV-Export: Admin {interaction.user.name} hat eine CSV-Datei der Teams für Event '{event['name']}' exportiert",
        guild=interaction.guild
    )
    
    # Sende Datei als Anhang
    await send_feedback(
        interaction,
        f"Hier ist die Teamliste für das Event '{event['name']}':",
        ephemeral=False,
        embed=None,
        view=None
    )
    
    # Da send_feedback nicht direkt Dateien unterstützt, müssen wir hier direkt followup verwenden
    await interaction.followup.send(file=discord.File(fp=csv_file, filename=filename))

# Admin-Commands
@bot.tree.command(name="admin_add_team", description="Fügt ein Team direkt zum Event oder zur Warteliste hinzu (nur für Orga-Team)")
@app_commands.describe(
    team_name="Name des Teams",
    size="Größe des Teams",
    discord_id="Discord ID des Team-Representatives (optional)",
    discord_name="Discord Name des Team-Representatives (optional)",
    force_waitlist="Team direkt auf die Warteliste setzen (True/False)"
)
async def add_team_command(
    interaction: discord.Interaction, 
    team_name: str, 
    size: int, 
    discord_id: str = None, 
    discord_name: str = None, 
    force_waitlist: bool = False
):
    """Fügt ein Team direkt zum Event oder zur Warteliste hinzu (Admin-Befehl)"""
    
    # Validiere Berechtigungen (nur Organisatoren)
    event, _ = await validate_command_context(interaction, required_role=ORGANIZER_ROLE, team_required=False)
    if not event:
        return

    # Versuche Discord ID zu konvertieren, wenn angegeben
    discord_user_id = None
    if discord_id:
        try:
            discord_user_id = int(discord_id.strip())
        except ValueError:
            await send_feedback(
                interaction,
                "Die Discord ID muss eine gültige Zahl sein."
            )
            return

    # Team mit der Admin-Funktion hinzufügen
    success = await admin_add_team(
        interaction, 
        team_name, 
        size, 
        discord_user_id=discord_user_id, 
        discord_username=discord_name, 
        force_waitlist=force_waitlist
    )
    
    if success:
        # Event-Anzeige aktualisieren
        channel = bot.get_channel(channel_id)
        if channel:
            await update_event_displays(channel=channel)
    else:
        # Fehlermeldung wird bereits von admin_add_team gesendet
        pass


@bot.tree.command(name="admin_team_edit", description="Bearbeitet die Größe eines Teams (nur für Orga-Team)")
@app_commands.describe(
    team_name="Name des Teams",
    new_size="Neue Größe des Teams",
    reason="Grund für die Änderung (optional)"
)
async def admin_team_edit_command(interaction: discord.Interaction, team_name: str, new_size: int, reason: str = None):
    """Bearbeitet die Größe eines Teams (Admin-Befehl)"""
    
    # Validiere Berechtigungen (nur Organisatoren)
    event, _ = await validate_command_context(interaction, required_role=ORGANIZER_ROLE, team_required=False)
    if not event:
        return

    # Teamgröße mit Admin-Rechten aktualisieren
    team_name = team_name.strip()
    success = await update_team_size(interaction, team_name, new_size, is_admin=True, reason=reason)
    
    if success:
        # Event-Anzeige aktualisieren
        channel = bot.get_channel(channel_id)
        if channel:
            await update_event_displays(channel=channel)
    else:
        # Fehlermeldung wird bereits von update_team_size gesendet
        pass


@bot.tree.command(name="admin_team_remove", description="Entfernt ein Team vom Event oder der Warteliste (nur für Orga-Team)")
@app_commands.describe(
    team_name="Name des Teams, das entfernt werden soll"
)
async def admin_team_remove_command(interaction: discord.Interaction, team_name: str):
    """Entfernt ein Team vom Event oder der Warteliste (Admin-Befehl)"""
    
    # Validiere Berechtigungen (nur Organisatoren)
    event, _ = await validate_command_context(interaction, required_role=ORGANIZER_ROLE, team_required=False)
    if not event:
        return

    team_name = team_name.strip()
    
    # Team mit Admin-Rechten abmelden
    success = await handle_team_unregistration(interaction, team_name, is_admin=True)
    
    if success:
        # Event-Anzeige aktualisieren
        channel = bot.get_channel(channel_id)
        if channel:
            await update_event_displays(channel=channel)


@bot.tree.command(name="admin_waitlist", description="Zeigt die vollständige Warteliste an (nur für Orga-Team)")
async def admin_waitlist_command(interaction: discord.Interaction):
    """Zeigt die vollständige Warteliste mit Details an (Admin-Befehl)"""
    
    # Validiere Berechtigungen (nur Organisatoren)
    event, _ = await validate_command_context(interaction, required_role=ORGANIZER_ROLE, team_required=False)
    if not event:
        return
    
    # Keine Warteliste vorhanden
    if not event.get('waitlist', []):
        await send_feedback(
            interaction,
            "Es sind aktuell keine Teams auf der Warteliste."
        )
        return
    
    # Warteliste formatieren
    waitlist_str = "## 📋 Warteliste\n\n"
    for idx, entry in enumerate(event['waitlist']):
        # Prüfe das Format der Wartelisten-Einträge
        if isinstance(entry, dict):
            # Dictionary-Format (neues Format)
            team_name = entry.get('team_name', 'Unbekannt')
            size = entry.get('size', 0)
            team_id = entry.get('team_id', 'N/A')
            waitlist_str += f"**{idx+1}.** {team_name} ({size} Spieler, Team-ID: {team_id})\n"
        elif isinstance(entry, tuple) and len(entry) >= 3:
            # Tupel-Format mit Team-ID (team_name, size, team_id)
            team_name, size, team_id = entry
            waitlist_str += f"**{idx+1}.** {team_name} ({size} Spieler, Team-ID: {team_id})\n"
        elif isinstance(entry, tuple) and len(entry) >= 2:
            # Tupel-Format ohne Team-ID (team_name, size)
            team_name, size = entry[:2]
            waitlist_str += f"**{idx+1}.** {team_name} ({size} Spieler)\n"
        else:
            # Unbekanntes Format
            waitlist_str += f"**{idx+1}.** {entry} (Format nicht erkannt)\n"
    
    # Warteliste als Embed senden
    embed = discord.Embed(
        title=f"Warteliste für {event['name']}",
        description=waitlist_str,
        color=discord.Color.orange()
    )
    
    embed.set_footer(text=f"Insgesamt {len(event['waitlist'])} Teams auf der Warteliste")
    
    await send_feedback(
        interaction,
        "Hier ist die vollständige Warteliste:",
        ephemeral=True,
        embed=embed
    )


@bot.tree.command(name="admin_user_assignments", description="Zeigt alle Benutzer-Team-Zuweisungen an (nur für Orga-Team)")
async def admin_user_assignments_command(interaction: discord.Interaction):
    """Zeigt alle Benutzer-Team-Zuweisungen an (Admin-Befehl)"""
    
    # Validiere Berechtigungen (nur Organisatoren)
    event, _ = await validate_command_context(interaction, required_role=ORGANIZER_ROLE, team_required=False)
    if not event:
        return
    
    global user_team_assignments
    
    # Keine Zuweisungen vorhanden
    if not user_team_assignments:
        await send_feedback(
            interaction,
            "Es sind aktuell keine Benutzer-Team-Zuweisungen vorhanden."
        )
        return
    
    # Zuweisungen formatieren
    assignments_str = "## 👥 Benutzer-Team-Zuweisungen\n\n"
    
    # Nach Teams gruppieren
    team_users = {}
    for user_id, team_name in user_team_assignments.items():
        if team_name not in team_users:
            team_users[team_name] = []
        
        # Versuche den Benutzer zu holen
        user = interaction.guild.get_member(int(user_id))
        user_display = f"<@{user_id}> ({user.display_name if user else 'Unbekannt'})"
        team_users[team_name].append(user_display)
    
    # Sortiere Teams alphabetisch
    for team_name in sorted(team_users.keys()):
        assignments_str += f"**{team_name}**:\n"
        for user_entry in team_users[team_name]:
            assignments_str += f"- {user_entry}\n"
        assignments_str += "\n"
    
    # Zuweisungen als Embed senden
    embed = discord.Embed(
        title="Benutzer-Team-Zuweisungen",
        description=assignments_str,
        color=discord.Color.blue()
    )
    
    embed.set_footer(text=f"Insgesamt {len(user_team_assignments)} Benutzer-Zuweisungen")
    
    await send_feedback(
        interaction,
        "Hier sind alle Benutzer-Team-Zuweisungen:",
        ephemeral=True,
        embed=embed
    )


@bot.tree.command(name="admin_get_user_id", description="Gibt die Discord ID eines Benutzers zurück (nur für Orga-Team)")
@app_commands.describe(
    user="Der Benutzer, dessen ID du erhalten möchtest"
)
async def admin_get_user_id_command(interaction: discord.Interaction, user: discord.User):
    """Gibt die Discord ID eines Benutzers zurück (Admin-Befehl)"""
    
    # Validiere Berechtigungen (nur Organisatoren)
    event, _ = await validate_command_context(interaction, required_role=ORGANIZER_ROLE, team_required=False)
    if not event:
        return
    
    # User ID und Details senden
    await send_feedback(
        interaction,
        f"### Benutzerinformationen für {user.mention}:\n\n"
        f"**Discord ID:** `{user.id}`\n"
        f"**Username:** {user.name}\n"
        f"**Joined Discord am:** {user.created_at.strftime('%d.%m.%Y')}\n"
        f"**Team:** {get_user_team(str(user.id)) or 'Kein Team zugewiesen'}",
        ephemeral=True
    )


@bot.tree.command(name="sync", description="Synchronisiert die Slash-Commands (nur für Orga-Team)")
@app_commands.describe(
    clear_cache="Ob der Discord-API-Cache vollständig gelöscht werden soll (empfohlen bei Problemen)"
)
async def sync_commands(interaction: discord.Interaction, clear_cache: bool = False):
    """Synchronisiert die Slash-Commands mit der Discord API"""
    # Kommandoausführung loggen
    logger.info(f"Slash-Command: /sync ausgeführt von {interaction.user.name} ({interaction.user.id}) in Kanal {interaction.channel.name} mit Parameter clear_cache={clear_cache}")
    
    # Validiere Berechtigungen (nur Organisatoren)
    if not has_role(interaction.user, ORGANIZER_ROLE):
        logger.warning(f"Berechtigungsfehler: {interaction.user.name} ({interaction.user.id}) hat versucht, /sync ohne ausreichende Berechtigungen zu verwenden")
        await send_feedback(
            interaction,
            f"Du benötigst die Rolle '{ORGANIZER_ROLE}', um diesen Befehl zu nutzen.",
            ephemeral=True
        )
        return
    
    # Bestätigungsnachricht senden
    await send_feedback(
        interaction,
        f"{'Lösche den Discord-API-Cache und s' if clear_cache else 'S'}ynchronisiere Slash-Commands mit der Discord API. Dies kann einen Moment dauern...",
        ephemeral=True
    )
    
    try:
        if clear_cache:
            # Alle Befehle vom Bot entfernen
            bot.tree.clear_commands(guild=None)
            # Änderungen an die API senden
            await bot.tree.sync()
            # Kurz warten
            await asyncio.sleep(2)
            # Befehle neu laden
            await bot.tree._set_current_commands(reload=True)
            # Erneut synchronisieren
            await bot.tree.sync()
            
            logger.info("Discord-API-Cache erfolgreich gelöscht und Commands neu synchronisiert")
        else:
            # Normale Synchronisierung ohne Cache-Löschung
            await bot.tree.sync()
        
        # Log-Eintrag für erfolgreiche Synchronisierung
        await send_to_log_channel(
            f"🔄 Slash-Commands: Admin {interaction.user.name} hat die Slash-Commands {'mit Cache-Löschung ' if clear_cache else ''}synchronisiert",
            level="INFO",
            guild=interaction.guild
        )
        
        await interaction.followup.send(
            f"Slash-Commands wurden erfolgreich {'mit Cache-Löschung ' if clear_cache else ''}synchronisiert!\n"
            f"Es kann bis zu einer Stunde dauern, bis alle Änderungen bei allen Nutzern sichtbar sind.\n\n"
            f"Tipp: Bei Problemen im Discord-Client hilft oft ein Neustart der Discord-App.",
            ephemeral=True
        )
    except Exception as e:
        logger.error(f"Fehler bei der Synchronisierung der Slash-Commands: {e}")
        await interaction.followup.send(f"Fehler bei der Synchronisierung: {e}", ephemeral=True)

@bot.tree.command(name="admin_help", description="Zeigt Hilfe zu Admin-Befehlen an (nur für Orga-Team)")
async def admin_help_command(interaction: discord.Interaction):
    """Zeigt Hilfe zu den verfügbaren Admin-Befehlen"""
    
    # Validiere Berechtigungen (nur Organisatoren)
    if not has_role(interaction.user, ORGANIZER_ROLE):
        await send_feedback(
            interaction,
            f"Du benötigst die Rolle '{ORGANIZER_ROLE}', um diesen Befehl zu nutzen.",
            ephemeral=True
        )
        return
    
    # Admin Befehle als Embed senden
    embed = discord.Embed(
        title="📋 Admin-Befehle für Event-Management",
        description="Hier ist eine Übersicht aller verfügbaren Admin-Befehle:",
        color=discord.Color.blue()
    )
    
    # Event-Verwaltungsbefehle
    embed.add_field(
        name="Event-Verwaltung",
        value=(
            "• `/event` - Erstellt ein neues Event\n"
            "• `/delete_event` - Löscht das aktuelle Event\n"
            "• `/open_reg` - Erhöht die maximale Teamgröße\n"
            "• `/close` - Schließt die Anmeldungen für das Event\n"
            "• `/update` - Aktualisiert die Event-Anzeige"
        ),
        inline=False
    )
    
    # Team-Verwaltungsbefehle
    embed.add_field(
        name="Team-Verwaltung",
        value=(
            "• `/admin_add_team` - Fügt ein Team direkt zum Event oder zur Warteliste hinzu\n"
            "• `/admin_team_edit` - Bearbeitet die Größe eines Teams\n"
            "• `/admin_team_remove` - Entfernt ein Team vom Event oder der Warteliste\n"
            "• `/reset_team_assignment` - Setzt die Team-Zuweisung eines Nutzers zurück"
        ),
        inline=False
    )
    
    # Informationsbefehle
    embed.add_field(
        name="Informationen & Tools",
        value=(
            "• `/admin_waitlist` - Zeigt die vollständige Warteliste mit Details an\n"
            "• `/admin_user_assignments` - Zeigt alle Benutzer-Team-Zuweisungen an\n"
            "• `/admin_get_user_id` - Gibt die Discord ID eines Benutzers zurück\n"
            "• `/export_csv` oder `/export_teams` - Exportiert die Teams als CSV-Datei\n"
            "• `/team_list` - Zeigt eine formatierte Liste aller Teams\n"
            "• `/find` - Findet ein Team oder einen Spieler im Event"
        ),
        inline=False
    )
    
    # Log-Verwaltungsbefehle
    embed.add_field(
        name="Log-Verwaltung & System",
        value=(
            "• `/export_log` - Exportiert die Log-Datei zum Download\n"
            "• `/import_log` - Importiert eine Log-Datei in das System\n"
            "• `/clear_log` - Leert die Log-Datei (erstellt vorher ein Backup)\n"
            "• `/clear_messages` - Löscht Nachrichten im Kanal mit Bestätigungsdialog\n"
            "• `/test` - Führt die Test-Suite aus (nur für Entwicklung und Debugging)"
        ),
        inline=False
    )
    
    # Konfigurationsbefehle
    embed.add_field(
        name="Konfiguration",
        value=(
            "• `/set_channel` - Setzt den aktuellen Channel für Event-Updates\n"
        ),
        inline=False
    )
    
    embed.set_footer(text="Alle Admin-Befehle erfordern die Rolle 'Orga-Team'")
    
    await send_feedback(
        interaction,
        "Hier ist eine Übersicht aller Admin-Befehle:",
        ephemeral=True,
        embed=embed
    )


# Log-Verwaltungsbefehle
@bot.tree.command(name="export_log", description="Exportiert die Log-Datei zum Download (nur für Orga-Team)")
async def export_log_command(interaction: discord.Interaction):
    """Exportiert die Log-Datei"""
    # Kommandoausführung loggen
    logger.info(f"Slash-Command: /export_log ausgeführt von {interaction.user.name} ({interaction.user.id}) in Kanal {interaction.channel.name}")
    
    # Validiere Berechtigungen (nur Organisatoren)
    if not has_role(interaction.user, ORGANIZER_ROLE):
        logger.warning(f"Berechtigungsfehler: {interaction.user.name} ({interaction.user.id}) hat versucht, /export_log ohne ausreichende Berechtigungen zu verwenden")
        await send_feedback(
            interaction,
            f"Du benötigst die Rolle '{ORGANIZER_ROLE}', um diesen Befehl zu nutzen.",
            ephemeral=True
        )
        return
    
    # Log-Datei exportieren
    result = export_log_file()
    
    if not result:
        await send_feedback(
            interaction,
            "Fehler beim Exportieren der Log-Datei. Bitte prüfe die Logs für Details.",
            ephemeral=True
        )
        return
    
    # Exportierte Datei senden
    await send_feedback(
        interaction,
        f"Hier ist die exportierte Log-Datei:",
        ephemeral=True
    )
    
    # Discord-Datei erstellen und senden
    file = discord.File(fp=result['buffer'], filename=result['filename'])
    await interaction.followup.send(file=file, ephemeral=True)
    
    # Log-Eintrag für erfolgreichen Export
    await send_to_log_channel(
        f"📥 Log-Export: Admin {interaction.user.name} hat die Log-Datei exportiert",
        level="INFO",
        guild=interaction.guild
    )

@bot.tree.command(name="clear_log", description="Löscht den Inhalt der Log-Datei (nur für Orga-Team)")
async def clear_log_command(interaction: discord.Interaction):
    """Löscht den Inhalt der Log-Datei"""
    # Kommandoausführung loggen
    logger.info(f"Slash-Command: /clear_log ausgeführt von {interaction.user.name} ({interaction.user.id}) in Kanal {interaction.channel.name}")
    
    # Validiere Berechtigungen (nur Organisatoren)
    if not has_role(interaction.user, ORGANIZER_ROLE):
        logger.warning(f"Berechtigungsfehler: {interaction.user.name} ({interaction.user.id}) hat versucht, /clear_log ohne ausreichende Berechtigungen zu verwenden")
        await send_feedback(
            interaction,
            f"Du benötigst die Rolle '{ORGANIZER_ROLE}', um diesen Befehl zu nutzen.",
            ephemeral=True
        )
        return
    
    # Bestätigungsabfrage
    class ClearLogConfirmationView(BaseConfirmationView):
        def __init__(self):
            super().__init__(title="Log-Datei löschen")
        
        @ui.button(label="Ja, Log löschen", style=discord.ButtonStyle.danger)
        async def confirm_callback(self, interaction: discord.Interaction, button: ui.Button):
            # Log-Datei leeren
            success = clear_log_file()
            
            if success:
                await send_feedback(
                    interaction,
                    "Die Log-Datei wurde erfolgreich geleert und ein Backup erstellt.",
                    ephemeral=True
                )
                
                # Log-Eintrag für erfolgreiche Löschung
                await send_to_log_channel(
                    f"🗑️ Log-Löschung: Admin {interaction.user.name} hat die Log-Datei geleert",
                    level="INFO",
                    guild=interaction.guild
                )
            else:
                await send_feedback(
                    interaction,
                    "Fehler beim Leeren der Log-Datei. Bitte prüfe die Logs für Details.",
                    ephemeral=True
                )
        
        @ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
        async def cancel_callback(self, interaction: discord.Interaction, button: ui.Button):
            await send_feedback(
                interaction,
                "Löschen der Log-Datei abgebrochen.",
                ephemeral=True
            )
    
    # Bestätigungsabfrage senden
    confirm_view = ClearLogConfirmationView()
    await send_feedback(
        interaction,
        "⚠️ **Warnung**: Bist du sicher, dass du die Log-Datei leeren möchtest?\n"
        "Ein Backup wird automatisch erstellt, aber die aktuelle Log-Datei wird geleert.",
        ephemeral=True,
        view=confirm_view
    )

@bot.tree.command(name="import_log", description="Importiert eine Log-Datei (nur für Orga-Team)")
@app_commands.describe(
    append="Ob die importierte Datei an die bestehende Log-Datei angehängt (True) oder die bestehende überschrieben werden soll (False)"
)
async def import_log_command(interaction: discord.Interaction, append: bool = True):
    """Importiert eine Log-Datei"""
    # Kommandoausführung loggen
    logger.info(f"Slash-Command: /import_log ausgeführt von {interaction.user.name} ({interaction.user.id}) in Kanal {interaction.channel.name} mit Parameter append={append}")
    
    # Validiere Berechtigungen (nur Organisatoren)
    if not has_role(interaction.user, ORGANIZER_ROLE):
        logger.warning(f"Berechtigungsfehler: {interaction.user.name} ({interaction.user.id}) hat versucht, /import_log ohne ausreichende Berechtigungen zu verwenden")
        await send_feedback(
            interaction,
            f"Du benötigst die Rolle '{ORGANIZER_ROLE}', um diesen Befehl zu nutzen.",
            ephemeral=True
        )
        return
    
    # Aufforderung zum Hochladen einer Datei
    await send_feedback(
        interaction,
        f"Bitte lade eine Log-Datei hoch. Der Inhalt wird {'an die bestehende Log-Datei angehängt' if append else 'die bestehende Log-Datei ersetzen'}.\n"
        f"Lade die Datei als Antwort auf diese Nachricht hoch.",
        ephemeral=True
    )
    
    # Warte auf den Upload
    try:
        response_message = await bot.wait_for(
            "message",
            check=lambda m: m.author == interaction.user and m.channel == interaction.channel and m.attachments,
            timeout=300  # 5 Minuten Timeout
        )
        
        # Prüfe, ob eine Datei angehängt wurde
        if not response_message.attachments:
            await send_feedback(
                interaction,
                "Keine Datei gefunden. Der Import wurde abgebrochen.",
                ephemeral=True
            )
            return
        
        # Hole die erste Datei
        attachment = response_message.attachments[0]
        
        # Prüfe die Dateigröße (max. 10 MB)
        if attachment.size > 10 * 1024 * 1024:
            await send_feedback(
                interaction,
                "Die Datei ist zu groß (max. 10 MB erlaubt). Der Import wurde abgebrochen.",
                ephemeral=True
            )
            return
        
        # Lade den Inhalt der Datei
        file_content = await attachment.read()
        
        # Importiere die Datei
        success = import_log_file(file_content, append)
        
        if success:
            await send_feedback(
                interaction,
                f"Die Log-Datei '{attachment.filename}' wurde erfolgreich importiert.",
                ephemeral=True
            )
            
            # Log-Eintrag für erfolgreichen Import
            await send_to_log_channel(
                f"📤 Log-Import: Admin {interaction.user.name} hat eine Log-Datei importiert (Anhangsmodus: {'Anhängen' if append else 'Überschreiben'})",
                level="INFO",
                guild=interaction.guild
            )
        else:
            await send_feedback(
                interaction,
                "Fehler beim Importieren der Log-Datei. Bitte prüfe die Logs für Details.",
                ephemeral=True
            )
        
        # Lösche die Upload-Nachricht
        try:
            await response_message.delete()
        except:
            pass
    
    except asyncio.TimeoutError:
        await send_feedback(
            interaction,
            "Zeitüberschreitung beim Warten auf den Datei-Upload. Der Import wurde abgebrochen.",
            ephemeral=True
        )


@bot.tree.command(name="clear_messages", description="Löscht die angegebene Anzahl der letzten Nachrichten im Kanal (nur für Orga-Team)")
@app_commands.describe(
    count="Anzahl der zu löschenden Nachrichten (max. 100)",
    reason="Optionaler Grund für die Löschung"
)
async def clear_messages_command(interaction: discord.Interaction, count: int, reason: str = None):
    """Löscht die angegebene Anzahl der letzten Nachrichten im Kanal (Admin-Befehl)"""
    # Validiere Berechtigungen (nur Organisatoren)
    if not has_role(interaction.user, ORGANIZER_ROLE):
        await send_feedback(interaction, f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können diese Aktion ausführen.", ephemeral=True)
        return
    
    # Validiere die Anzahl der zu löschenden Nachrichten
    if count <= 0:
        await send_feedback(interaction, "Die Anzahl der zu löschenden Nachrichten muss größer als 0 sein.", ephemeral=True)
        return
    
    if count > 100:
        await send_feedback(interaction, "Aus Sicherheitsgründen können maximal 100 Nachrichten gleichzeitig gelöscht werden.", ephemeral=True)
        return
    
    class ClearMessagesConfirmationView(BaseConfirmationView):
        def __init__(self, count, reason):
            super().__init__(title="Nachrichten löschen")
            self.count = count
            self.reason = reason
        
        @ui.button(label="Ja, löschen", style=discord.ButtonStyle.danger)
        async def confirm_callback(self, interaction: discord.Interaction, button: ui.Button):
            if self.check_response(interaction):
                await self.handle_already_responded(interaction)
                return
            
            try:
                # Muss zuerst mit defer antworten, da das Löschen länger dauern kann
                await interaction.response.defer(ephemeral=True)
                
                # Nachrichten löschen
                deleted = await interaction.channel.purge(limit=self.count)
                
                # Feedback senden
                reason_text = f" (Grund: {self.reason})" if self.reason else ""
                await interaction.followup.send(
                    f"✅ {len(deleted)} Nachrichten wurden gelöscht{reason_text}.",
                    ephemeral=True
                )
                
                # Log-Eintrag
                log_message = f"🗑️ {len(deleted)} Nachrichten wurden in Kanal #{interaction.channel.name} gelöscht durch {interaction.user.name} ({interaction.user.id})"
                if self.reason:
                    log_message += f" - Grund: {self.reason}"
                
                logger.warning(log_message)
                await send_to_log_channel(log_message, level="WARNING", guild=interaction.guild)
                
            except discord.errors.Forbidden:
                await interaction.followup.send(
                    "❌ Fehlende Berechtigung zum Löschen von Nachrichten.",
                    ephemeral=True
                )
            except Exception as e:
                await interaction.followup.send(
                    f"❌ Fehler beim Löschen der Nachrichten: {e}",
                    ephemeral=True
                )
                logger.error(f"Fehler beim Löschen von Nachrichten: {e}")
        
        @ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
        async def cancel_callback(self, interaction: discord.Interaction, button: ui.Button):
            if self.check_response(interaction):
                await self.handle_already_responded(interaction)
                return
                
            await send_feedback(interaction, "Löschvorgang abgebrochen.", ephemeral=True)
    
    # Bestätigungsdialog anzeigen
    reason_text = f"\nGrund: **{reason}**" if reason else ""
    embed = discord.Embed(
        title="⚠️ Nachrichten löschen?",
        description=f"Bist du sicher, dass du **{count} Nachrichten** in diesem Kanal löschen möchtest?{reason_text}\n\n"
                   f"Diese Aktion kann nicht rückgängig gemacht werden!",
        color=discord.Color.red()
    )
    
    # Erstelle die Bestätigungsansicht
    view = ClearMessagesConfirmationView(count, reason)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="test", description="Führt die Test-Suite aus (nur für Orga-Team)")
async def test_command(interaction: discord.Interaction):
    """Führt die Test-Suite aus der Test/test.py auf dem Discord aus"""
    # Validiere Berechtigungen (nur Organisatoren)
    if not has_role(interaction.user, ORGANIZER_ROLE):
        await send_feedback(interaction, f"Nur Mitglieder mit der Rolle '{ORGANIZER_ROLE}' können diese Aktion ausführen.", ephemeral=True)
        return
    
    try:
        # Informiere den Benutzer, dass Tests gestartet werden
        await send_feedback(
            interaction,
            "🧪 **Test-Suite wird gestartet...**\n"
            "Dies kann einige Sekunden dauern. Die Ergebnisse werden als Datei zurückgesendet.",
            ephemeral=True
        )
        
        # Importiere test.py Funktionen manuell über sys.path
        import os
        current_dir = os.getcwd()
        test_module_path = os.path.join(current_dir, "Test")
        sys.path.insert(0, test_module_path)
        
        # Import von test-Funktionen
        try:
            from test import run_test_suite
        except ImportError:
            # Alternative: Direkt auf das Test-Skript zugreifen
            logger.warning("Konnte run_test_suite nicht importieren, versuche direkten Zugriff")
            import subprocess
            
            def run_test_suite():
                """Ersatz-Funktion, die das test.py Skript direkt aufruft"""
                result = subprocess.run(["python", "Test/test.py"], capture_output=True, text=True)
                if result.returncode != 0:
                    logger.warning(f"Test-Script beendet mit Exit-Code {result.returncode}")
                    if result.stderr:
                        logger.error(f"Test-Fehlermeldung: {result.stderr}")
                return result.stdout
        
        # Stelle globale Variablen für die Daten-Wiederherstellung sicher
        global event_data, user_team_assignments, channel_id
        
        # Sichern der aktuellen Daten
        event_data_backup = copy.deepcopy(event_data)
        user_team_assignments_backup = copy.deepcopy(user_team_assignments)
        
        # Umleitung der stdout in eine StringIO, um die Ausgabe zu erfassen
        original_stdout = sys.stdout
        test_output = io.StringIO()
        sys.stdout = test_output
        
        try:
            # Führe die Test-Suite aus, aber mit Timeout
            import threading
            import time
            
            def run_test_with_timeout():
                try:
                    run_test_suite()
                except Exception as e:
                    logger.error(f"Fehler in der Test-Suite: {e}")
            
            # Starte Test in separatem Thread
            test_thread = threading.Thread(target=run_test_with_timeout)
            test_thread.daemon = True
            test_thread.start()
            
            # Warte maximal 30 Sekunden
            test_thread.join(timeout=30)
            
            # Prüfe, ob der Test noch läuft
            if test_thread.is_alive():
                logger.warning("Test-Suite Timeout nach 30 Sekunden - Test wird abgebrochen")
                # Test-Ausgabe trotzdem abrufen
                output = test_output.getvalue() + "\n\n*** TIMEOUT: Test wurde nach 30 Sekunden abgebrochen! ***"
            else:
                # Test-Ausgabe abrufen
                output = test_output.getvalue()
            
            # Loggen des Ergebnisses
            logger.info(f"Test-Suite ausgeführt von {interaction.user.name} ({interaction.user.id})")
            
            # Ausgabe für Log hinzufügen
            log_message = f"🧪 Test-Suite ausgeführt von {interaction.user.name} ({interaction.user.id})"
            await send_to_log_channel(log_message, level="INFO", guild=interaction.guild)
            
            # Erstelle eine temporäre Datei mit den Testergebnissen
            buffer = io.BytesIO(output.encode('utf-8'))
            buffer.seek(0)
            
            # Sende die Datei als Attachment
            file = discord.File(fp=buffer, filename="test_results.txt")
            await interaction.followup.send(
                content="✅ **Test-Suite abgeschlossen!**\nHier sind die Ergebnisse:",
                file=file,
                ephemeral=True
            )
            
        except Exception as e:
            # Fehlerbehandlung
            error_message = f"❌ **Fehler bei der Ausführung der Test-Suite:**\n```{str(e)}```"
            logger.error(f"Fehler bei der Ausführung der Test-Suite: {e}")
            await interaction.followup.send(content=error_message, ephemeral=True)
        finally:
            # Zurücksetzen von stdout und Wiederherstellung der ursprünglichen Daten
            sys.stdout = original_stdout
            event_data = event_data_backup
            user_team_assignments = user_team_assignments_backup
            
            # Speichere die ursprünglichen Daten
            save_data(event_data, channel_id, user_team_assignments)
    
    except Exception as e:
        # Allgemeine Fehlerbehandlung
        error_message = f"❌ **Fehler beim Starten der Test-Suite:**\n```{str(e)}```"
        logger.error(f"Fehler beim Starten der Test-Suite: {e}")
        await send_feedback(interaction, error_message, ephemeral=True)

# Start the bot
if __name__ == "__main__":
    logger.info("Starting bot...")
    bot.run(TOKEN)