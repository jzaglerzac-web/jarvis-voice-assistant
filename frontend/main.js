// Jarvis V2 — Frontend
const orb = document.getElementById('orb');
const status = document.getElementById('status');
const transcript = document.getElementById('transcript');

let ws;
let audioQueue = [];
let isPlaying = false;
let audioUnlocked = false;
let shutDown = false;
let currentAudio = null;
let interrupted = false;

// Unlock audio on ANY user interaction
function unlockAudio() {
    if (!audioUnlocked) {
        const silent = new Audio('data:audio/mp3;base64,SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4Ljc2LjEwMAAAAAAAAAAAAAAA//tQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAABhgC7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7//////////////////////////////////////////////////////////////////8AAAAATGF2YzU4LjEzAAAAAAAAAAAAAAAAJAAAAAAAAAAAAYZNIGPkAAAAAAAAAAAAAAAAAAAA//tQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAABhgC7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7//////////////////////////////////////////////////////////////////8AAAAATGF2YzU4LjEzAAAAAAAAAAAAAAAAJAAAAAAAAAAAAYZNIGPkAAAAAAAAAAAAAAAAAAAA');
        silent.play().then(() => {
            audioUnlocked = true;
            console.log('[jarvis] Audio unlocked');
        }).catch(() => {});
    }
}
document.addEventListener('click', unlockAudio, { once: false });
document.addEventListener('touchstart', unlockAudio, { once: false });
document.addEventListener('keydown', unlockAudio, { once: false });


// Sleep mode: after Jarvis has spoken he listens for SLEEP_AFTER_MS, then only
// reacts to "Hallo Jarvis" (and "ZAC aus") until he is woken up again.
const SLEEP_AFTER_MS = 30000;
const WAKE_PATTERN = /hallo\s+(jarvis|jarvic|javis|jarwis|tschavis|chavis)\b[,.!?\s]*/i;
const SHUTDOWN_PATTERN = /\b(zac|zack|zak)\s*aus\b/i;
// "mach langsam" (also "mach mal langsam") interrupts Jarvis
const STOP_PATTERN = /mach(e)?\s+(mal\s+)?langsam/i;
// Speech heard this soon after Jarvis stopped talking is the tail of his own voice
const ECHO_GRACE_MS = 1500;

let asleep = false;
let sleepTimer = null;
let lastJarvisText = '';
let playbackEndedAt = 0;

function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onopen = () => {
        console.log('[jarvis] WebSocket connected');
        status.textContent = 'Klicke einmal irgendwo, dann spricht Jarvis.';
        setOrbState('thinking');
        asleep = false;
        setMusicVolume('active', 12);
        ws.send(JSON.stringify({ text: 'Jarvis activate' }));
    };
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'response') {
            if (interrupted) return;
            clearTimeout(sleepTimer);
            addTranscript('jarvis', data.text);
            lastJarvisText += ' ' + data.text;
            if (data.audio && data.audio.length > 0) {
                queueAudio(data.audio);
            } else if (data.text) {
                // No ElevenLabs audio (e.g. quota used up): the browser's own voice speaks instead
                queueAudio({ text: data.text });
            } else {
                setOrbState('idle');
                setTimeout(startListening, 500);
            }
        } else if (data.type === 'shutdown') {
            shutDown = true;
            clearTimeout(sleepTimer);
            status.textContent = 'Jarvis wurde beendet.';
        } else if (data.type === 'status') {
            status.textContent = data.text;
        }
    };
    ws.onclose = () => {
        if (shutDown) { setOrbState('idle'); return; }
        status.textContent = 'Verbindung verloren...';
        setTimeout(connect, 3000);
    };
}

// Spotify volume via the server ("active" while awake, "sleep" in sleep mode).
// Spotify may still be starting, so retry a few times.
function setMusicVolume(mode, retries = 0) {
    fetch(`/api/music-volume?mode=${mode}`, { method: 'POST' })
        .then(r => r.json())
        .then(res => {
            if (!res.ok && retries > 0) setTimeout(() => setMusicVolume(mode, retries - 1), 5000);
        })
        .catch(() => {});
}

function queueAudio(item) {
    audioQueue.push(item);
    if (!isPlaying) playNext();
}

function playNext() {
    if (audioQueue.length === 0) {
        finishedSpeaking();
        return;
    }
    isPlaying = true;
    setOrbState('speaking');
    status.textContent = '';
    // Keep listening while Jarvis speaks so "mach langsam" can interrupt him
    if (!isListening) startListening();

    const item = audioQueue.shift();
    if (typeof item === 'object') {
        speakWithBrowser(item.text);
        return;
    }
    const bytes = Uint8Array.from(atob(item), c => c.charCodeAt(0));
    const blob = new Blob([bytes], { type: 'audio/mpeg' });
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    currentAudio = audio;
    audio.onended = () => { URL.revokeObjectURL(url); playNext(); };
    audio.onerror = () => { URL.revokeObjectURL(url); playNext(); };
    audio.play().catch(err => {
        console.warn('[jarvis] Autoplay blocked, waiting for click...');
        status.textContent = 'Klicke irgendwo damit Jarvis sprechen kann.';
        setOrbState('idle');
        // Wait for click then retry
        document.addEventListener('click', function retry() {
            document.removeEventListener('click', retry);
            audio.play().then(() => {
                setOrbState('speaking');
                status.textContent = '';
            }).catch(() => playNext());
        });
    });
}

function finishedSpeaking() {
    isPlaying = false;
    currentAudio = null;
    playbackEndedAt = Date.now();
    // Throw away what the mic picked up of Jarvis' own voice
    if (isListening) recognition.abort();
    if (asleep) return;
    setOrbState('listening');
    status.textContent = '';
    armSleepTimer();
}

// Fallback voice: the browser's built-in German speech synthesis
function speakWithBrowser(text) {
    if (!window.speechSynthesis) { playNext(); return; }
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'de-DE';
    const voices = speechSynthesis.getVoices().filter(v => v.lang.startsWith('de'));
    // Prefer a male German voice (Jarvis), otherwise any German one
    utterance.voice = voices.find(v => /stefan|conrad|killian|male|mann/i.test(v.name)) || voices[0] || null;
    utterance.rate = 1.05;
    utterance.onend = () => { if (isPlaying) playNext(); };
    utterance.onerror = () => { if (isPlaying) playNext(); };
    speechSynthesis.speak(utterance);
}

function armSleepTimer() {
    clearTimeout(sleepTimer);
    sleepTimer = setTimeout(goToSleep, SLEEP_AFTER_MS);
}

function goToSleep() {
    if (isPlaying || shutDown) return;
    asleep = true;
    setOrbState('idle');
    status.textContent = 'Ruhemodus – sag „Hallo Jarvis“.';
    setMusicVolume('sleep');
}

function wakeUp(rest) {
    asleep = false;
    interrupted = false;
    setMusicVolume('active');
    if (rest) {
        sendToJarvis(rest);
    } else {
        lastJarvisText = 'Ja, Sir?';
        queueAudio({ text: 'Ja, Sir?' });
    }
}

// True if the heard text is mostly words Jarvis just said himself
function isEcho(text) {
    const words = text.toLowerCase().match(/[a-zäöüß]{3,}/g) || [];
    if (words.length === 0) return false;
    const said = new Set((lastJarvisText.toLowerCase().match(/[a-zäöüß]{3,}/g) || []));
    const overlap = words.filter(w => said.has(w)).length;
    return overlap / words.length >= 0.6;
}

function sendToJarvis(text) {
    clearTimeout(sleepTimer);
    interrupted = false;
    lastJarvisText = '';
    addTranscript('user', text);
    setOrbState('thinking');
    status.textContent = 'Jarvis denkt nach...';
    ws.send(JSON.stringify({ text }));
}

// Speech Recognition
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition;
let isListening = false;

if (SpeechRecognition) {
    recognition = new SpeechRecognition();
    recognition.lang = 'de-DE';
    recognition.continuous = true;
    // Interim results let "mach langsam" stop Jarvis before the sentence is finished
    recognition.interimResults = true;

    recognition.onresult = (event) => {
        const last = event.results[event.results.length - 1];
        const heard = last[0].transcript.trim();
        if (!heard) return;

        if (isPlaying) {
            // The mic also hears Jarvis now; only "mach langsam" and "ZAC aus" count
            if (STOP_PATTERN.test(heard)) stopSpeaking();
            else if (last.isFinal && SHUTDOWN_PATTERN.test(heard)) { stopSpeaking(); sendToJarvis(heard); }
            return;
        }
        if (!last.isFinal) return;
        if (STOP_PATTERN.test(heard)) return;

        if (asleep) {
            const wake = heard.match(WAKE_PATTERN);
            if (wake) wakeUp(heard.slice(wake.index + wake[0].length).trim());
            else if (SHUTDOWN_PATTERN.test(heard)) sendToJarvis(heard);
            return;
        }

        if (Date.now() - playbackEndedAt < ECHO_GRACE_MS || isEcho(heard)) {
            console.log('[jarvis] Eigene Stimme ignoriert:', heard);
            return;
        }
        // "Hallo Jarvis, ..." while awake: just use what comes after it
        const wake = heard.match(WAKE_PATTERN);
        const text = wake ? heard.slice(wake.index + wake[0].length).trim() : heard;
        if (text) sendToJarvis(text);
        else armSleepTimer();
    };

    recognition.onend = () => {
        isListening = false;
        setTimeout(startListening, 300);
    };

    recognition.onerror = (event) => {
        isListening = false;
        if (event.error === 'no-speech' || event.error === 'aborted') {
            setTimeout(startListening, 300);
        } else {
            setTimeout(startListening, 1000);
        }
    };
}

function startListening() {
    if (shutDown || isListening) return;
    try {
        recognition.start();
        isListening = true;
        if (!isPlaying && !asleep) {
            setOrbState('listening');
            status.textContent = '';
        }
    } catch(e) {}
}

function stopSpeaking() {
    audioQueue = [];
    if (currentAudio) {
        currentAudio.pause();
        currentAudio = null;
    }
    if (window.speechSynthesis) speechSynthesis.cancel();
    // Drop answers that are still on their way until the user says something new
    interrupted = true;
    addTranscript('user', 'mach langsam');
    finishedSpeaking();
    status.textContent = 'Unterbrochen.';
}

orb.addEventListener('click', () => {
    if (isPlaying) return;
    if (asleep) { wakeUp(''); return; }
    if (isListening) {
        recognition.stop();
        isListening = false;
        setOrbState('idle');
        status.textContent = 'Pausiert. Klicke zum Fortsetzen.';
    } else {
        startListening();
    }
});

function setOrbState(state) { orb.className = state; }

function addTranscript(role, text) {
    const div = document.createElement('div');
    div.className = role;
    div.textContent = role === 'user' ? `Du: ${text}` : `Jarvis: ${text}`;
    transcript.appendChild(div);
    transcript.scrollTop = transcript.scrollHeight;
}

connect();
