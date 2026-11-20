# Transcriber (beta)

A Windows app that turns audio and video into editable text with Deepgram.

- Transcribe files, links (Dropbox, Google Drive, direct file links) or your microphone
- Edit with the recording playing: each word lights up as it's spoken
- Speakers, find and replace, undo, bookmarks, folders
- Write-ups with Google Gemini: summaries, chapters, sermon notes, meeting minutes
- Export to Word, PDF, text, subtitles (SRT/VTT), Markdown or JSON

Each person uses their own Deepgram key (and, for write-ups, their own Gemini key).
Keys are stored only on that computer.

## Download

Get `Transcriber-Setup-<version>.exe` from [Releases](../../releases). The installer
isn't code-signed yet, so Windows may show "Windows protected your PC": choose
**More info → Run anyway**.

## Build

Needs Python 3.12, `pip install pyinstaller pywebview fpdf2`, and Inno Setup 6. Run `build.bat`.
