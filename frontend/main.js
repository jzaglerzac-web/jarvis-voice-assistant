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

function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onopen = () => {
        console.log('[jarvis] WebSocket connected');
        status.textContent = 'Klicke einmal irgendwo, dann spricht Jarvis.';
        setOrbState('thinking');
        ws.send(JSON.stringify({ text: 'Jarvis activate' }));
    };
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'response') {
            if (interrupted) return;
            addTranscript('jarvis', data.text);
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

function queueAudio(base64Audio) {
    audioQueue.push(base64Audio);
    if (!isPlaying) playNext();
}

function playNext() {
    if (audioQueue.length === 0) {
        isPlaying = false;
        setOrbState('listening');
        status.textContent = '';
        setTimeout(startListening, 500);
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
    const b64 = item;
    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
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
        if (STOP_PATTERN.test(heard)) {
            if (isPlaying) stopSpeaking();
            return;
        }
        // While Jarvis speaks, the mic also hears him; only "mach langsam" and "ZAC aus" count then
        if (last.isFinal && isPlaying && /\b(zac|zack|zak)\s*aus\b/i.test(heard)) stopSpeaking();
        if (last.isFinal && !isPlaying) {
            const text = heard;
            if (text) {
                interrupted = false;
                addTranscript('user', text);
                setOrbState('thinking');
                status.textContent = 'Jarvis denkt nach...';
                ws.send(JSON.stringify({ text }));
            }
        }
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
        if (!isPlaying) {
            setOrbState('listening');
            status.textContent = '';
        }
    } catch(e) {}
}

// "mach langsam" (also "mach mal langsam") interrupts Jarvis
const STOP_PATTERN = /mach(e)?\s+(mal\s+)?langsam/i;

function stopSpeaking() {
    audioQueue = [];
    if (currentAudio) {
        currentAudio.pause();
        currentAudio = null;
    }
    if (window.speechSynthesis) speechSynthesis.cancel();
    isPlaying = false;
    // Drop answers that are still on their way until the user says something new
    interrupted = true;
    addTranscript('user', 'mach langsam');
    setOrbState('listening');
    status.textContent = 'Unterbrochen.';
}

orb.addEventListener('click', () => {
    if (isPlaying) return;
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
