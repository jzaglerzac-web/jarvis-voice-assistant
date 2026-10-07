# ElevenLabs fuer Jarvis einrichten

ElevenLabs ist Jarvis' Stimme: `server.py` schickt jede Antwort an die
ElevenLabs-API (`/v1/text-to-speech/{voice_id}`, Modell `eleven_turbo_v2_5`)
und spielt das MP3 im Browser ab. Ohne gueltigen Key laeuft Jarvis weiter,
antwortet aber nur als Text.

Du brauchst am Ende zwei Werte in `config.json`:

```json
"elevenlabs_api_key": "sk_...",
"elevenlabs_voice_id": "onwK4e9ZLuTAKqWW03F9"
```

---

## 1. Account anlegen

1. https://elevenlabs.io oeffnen, **Sign up** (Google-Login oder E-Mail).
2. Die Fragen beim Onboarding beliebig beantworten, Plan **Free** waehlen.
   Keine Kreditkarte noetig.

## 2. API-Key erstellen

1. Links unten auf dein Profil, dann **Developers** (bzw. direkt
   https://elevenlabs.io/app/developers/api-keys).
2. **Create API Key**, Name z.B. `Jarvis`.
3. Berechtigungen: mindestens **Text to Speech** auf *Access* und
   **Voices** auf *Read* stellen (fuer das Testskript). Alles andere darf aus bleiben.
4. Optional ein Credit-Limit setzen (z.B. 10.000), damit nichts ueberzieht.
5. Key kopieren (`sk_...`). Er wird nur einmal angezeigt.

Den Key nur in `config.json` eintragen. Die Datei ist gitignored und landet
nicht auf GitHub.

## 3. Stimme waehlen

Auf dem Free-Plan funktionieren ueber die API nur die **Standard-Stimmen**
(Kategorie `premade`). Stimmen aus der Voice Library brauchen einen bezahlten Plan.
Alle Standard-Stimmen sprechen mit `eleven_turbo_v2_5` auch Deutsch.

Gute Kandidaten fuer Jarvis:

| Stimme | Voice ID | Charakter |
|---|---|---|
| Daniel | `onwK4e9ZLuTAKqWW03F9` | britisch, ruhig, Nachrichtensprecher (am naechsten an Jarvis) |
| George | `JBFqnCBsd6RMkjVDRZzb` | britisch, warm, erzaehlend |
| Brian | `nPczCjzI2devNBz1zQrb` | amerikanisch, tief |

Die Voice ID der Standardvorgabe in `server.py` (`rDmv3mOhK6TnhYWckFaD`)
stammt aus dem Original-Repo und ist eine Library-Stimme. Auf dem
Free-Plan bitte eine eigene ID eintragen.

Alle Stimmen, die dein Key nutzen darf, listet:

```
python scripts/test-elevenlabs.py --voices
```

## 4. Verbindung testen

```
python scripts/test-elevenlabs.py
```

Bei Erfolg liegt `test.mp3` im Jarvis-Ordner ("Guten Tag, Sir. Alle Systeme
sind bereit."). Haeufige Fehler:

| Antwort | Bedeutung |
|---|---|
| 401 `invalid_api_key` | Key falsch kopiert |
| 401 `missing_permissions` | Key hat keine Text-to-Speech-Berechtigung |
| 402 / `paid_plan_required` | Library-Stimme auf Free-Plan, Standard-Stimme nehmen |
| 401 `detected_unusual_activity` | Free-Plan ueber VPN/Proxy gesperrt, VPN aus oder Starter-Plan |
| 429 / `quota_exceeded` | Monatliche Credits aufgebraucht |

## 5. Plaene und Limits (Stand Oktober 2026)

| Plan | Preis | Credits/Monat | Hinweis |
|---|---|---|---|
| Free | 0 $ | 10.000 | keine kommerzielle Nutzung, nur Standard-Stimmen per API |
| Starter | ca. 6 $ (oft Rabatt im 1. Monat) | 30.000 | Library-Stimmen, Instant Voice Cloning |
| Creator | ca. 22 $ (oft Rabatt im 1. Monat) | ~120.000 | Professional Voice Cloning |
| Pro | 99 $ | 600.000 | |

Aktuelle Preise: https://elevenlabs.io/pricing

Ein Credit entspricht ungefaehr einem Zeichen; die Turbo/Flash-Modelle
verbrauchen weniger (etwa die Haelfte). Jarvis' Antworten sind kurz
(1 bis 3 Saetze, ~150 Zeichen), dazu kommt die Begruessung beim Start.
Grob gerechnet reichen die 10.000 Free-Credits fuer **60 bis 120 Antworten
im Monat**. Wer Jarvis taeglich nutzt, landet beim Starter-Plan.
