/* OpenCohost phone page. Plain ES2017, no dependencies, same-origin requests only:
   POST /pair, POST /turn, GET /reply. The device token lives in localStorage and is
   only ever sent in the Authorization header. */
(function () {
  'use strict';

  var STRINGS = {
    es: {
      notLinked: 'Sin vincular',
      ready: 'Listo',
      listening: 'Escuchando...',
      sending: 'Enviando...',
      waiting: 'Esperando a Kira...',
      speaking: 'Kira esta hablando',
      noReply: 'Kira no respondio',
      error: 'Error de conexion',
      enterCode: 'Escribe el codigo de vinculacion que aparece en la PC',
      pair: 'Vincular',
      deviceName: 'Nombre de este celular',
      defaultName: 'Celular',
      badCode: 'Codigo no aceptado. Genera uno nuevo en la PC.',
      talk: 'Manten apretado para hablar',
      heard: 'Lo que dijiste',
      reply: 'Kira dice',
      textPlaceholder: 'O escribe tu mensaje',
      send: 'Enviar',
      stop: 'Detener voz',
      unlink: 'Desvincular este celular',
      noSpeech: 'Este navegador no tiene reconocimiento de voz. Usa el cuadro de texto.',
      speechFail: 'No se pudo usar el microfono. Usa el cuadro de texto.',
      wait: 'Espera un momento',
      notReady: 'Kira no esta lista',
      tooLong: 'Mensaje demasiado largo',
      noSpeak: 'Este navegador no puede leer en voz alta; la respuesta se muestra abajo.',
      avatarHide: 'Ocultar avatar',
      avatarShow: 'Mostrar avatar'
    },
    en: {
      notLinked: 'Not linked',
      ready: 'Ready',
      listening: 'Listening...',
      sending: 'Sending...',
      waiting: 'Waiting for Kira...',
      speaking: 'Kira is speaking',
      noReply: 'Kira did not reply',
      error: 'Connection error',
      enterCode: 'Type the pairing code shown on the PC',
      pair: 'Link',
      deviceName: 'Name for this phone',
      defaultName: 'Phone',
      badCode: 'Code not accepted. Generate a new one on the PC.',
      talk: 'Hold to talk',
      heard: 'What you said',
      reply: 'Kira says',
      textPlaceholder: 'Or type your message',
      send: 'Send',
      stop: 'Stop voice',
      unlink: 'Unlink this phone',
      noSpeech: 'This browser has no speech recognition. Use the text box.',
      speechFail: 'The microphone could not be used. Use the text box.',
      wait: 'Wait a moment',
      notReady: 'Kira is not ready',
      tooLong: 'Message too long',
      noSpeak: 'This browser cannot read aloud; the reply is shown below.',
      avatarHide: 'Hide avatar',
      avatarShow: 'Show avatar'
    }
  };

  var POLL_MS = 700;
  var POLL_GIVE_UP_MS = 95000;
  var TOKEN_KEY = 'opencohost_gateway_token';
  var AVATAR_HIDDEN_KEY = 'opencohost_avatar_hidden';

  var navLang = (navigator.language || 'es').toLowerCase();
  var isSpanish = navLang.indexOf('es') === 0;
  var S = isSpanish ? STRINGS.es : STRINGS.en;
  var recLang = isSpanish ? 'es-MX' : (navigator.language || 'en-US');

  function $(id) { return document.getElementById(id); }

  var token = null;
  var busy = false;
  var recognition = null;
  var heardText = '';
  var unlocked = false;
  var pollTimer = null;
  var pollDeadline = 0;

  function storeGet() { try { return window.localStorage.getItem(TOKEN_KEY); } catch (e) { return null; } }
  function storeSet(v) { try { window.localStorage.setItem(TOKEN_KEY, v); } catch (e) { /* private mode */ } }
  function storeClear() { try { window.localStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ } }

  // ---- avatar contract ------------------------------------------------------
  // DOM events consumed by /avatar.js; phone.js itself never loads the avatar.
  //   kira:auth   {token: string|null}   paired (token) or unpaired / auth lost (null)
  //   kira:speech {phase: 'start'|'boundary'|'end'}
  //   kira:state  {state: 'idle'|'thinking'|'listening'}

  function emit(name, detail) {
    try { document.dispatchEvent(new CustomEvent(name, { detail: detail })); } catch (e) { /* ignore */ }
  }

  function announceAuth() {
    function send() { emit('kira:auth', { token: token || null }); }
    // The avatar module runs after parsing; wait so it is already listening.
    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', send, { once: true }); } else { send(); }
  }

  function announceSpeech(phase) { emit('kira:speech', { phase: phase }); }
  function announceState(state) { emit('kira:state', { state: state }); }

  function avatarHiddenGet() { try { return window.localStorage.getItem(AVATAR_HIDDEN_KEY) === '1'; } catch (e) { return false; } }
  function avatarHiddenSet(v) { try { window.localStorage.setItem(AVATAR_HIDDEN_KEY, v ? '1' : '0'); } catch (e) { /* private mode */ } }

  function applyAvatarVisibility() {
    var hidden = avatarHiddenGet();
    var t = $('avatarToggle');
    $('avatarWrap').hidden = hidden;
    t.textContent = '\u263A';
    t.dataset.off = hidden ? '1' : '0';
    t.setAttribute('aria-label', hidden ? S.avatarShow : S.avatarHide);
    t.title = hidden ? S.avatarShow : S.avatarHide;
  }

  function setStatus(text, isError) {
    var el = $('status');
    el.textContent = text;
    el.className = isError ? 'error' : '';
    // The idle text is never shown: the pill only appears while something is happening.
    el.dataset.state = (text === S.ready || !text) ? 'idle' : 'busy';
    // Stop is only useful while Kira is thinking or speaking.
    $('stop').hidden = !(text === S.sending || text === S.waiting || text === S.speaking);
  }

  function showPairing() {
    token = null;
    document.body.dataset.view = 'pairing';
    $('main').hidden = true;
    $('pairing').hidden = false;
    setStatus(S.notLinked);
    announceAuth();
  }

  function showMain() {
    document.body.dataset.view = 'main';
    $('pairing').hidden = true;
    $('main').hidden = false;
    setStatus(S.ready);
    announceAuth();
  }

  function authHeaders() {
    return { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token };
  }

  // ---- pairing --------------------------------------------------------------

  function normalizeCode(raw) { return String(raw || '').replace(/[\s-]+/g, '').toUpperCase(); }

  function pairWith(code) {
    var name = window.prompt(S.deviceName, S.defaultName) || S.defaultName;
    fetch('/pair', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: normalizeCode(code), name: name })
    }).then(function (res) {
      if (res.status === 401) { setStatus(S.badCode, true); return null; }
      if (!res.ok) { setStatus(S.error, true); return null; }
      return res.json();
    }).then(function (data) {
      if (!data || !data.token) { return; }
      token = data.token;
      storeSet(token);
      showMain();
    }).catch(function () { setStatus(S.error, true); });
  }

  // Reads the pairing code from the URL fragment and removes it from the address bar.
  function consumeHashCode() {
    var m = /[#&]code=([^&]*)/.exec(window.location.hash || '');
    if (!m) { return null; }
    try { window.history.replaceState(null, '', window.location.pathname + window.location.search); } catch (e) { /* ignore */ }
    try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
  }

  // ---- speech out -----------------------------------------------------------

  var synth = window.speechSynthesis || null;
  var voices = [];

  function loadVoices() { if (synth) { voices = synth.getVoices() || []; } }

  function pickVoice() {
    var want = recLang.toLowerCase();
    var base = want.split('-')[0];
    var i;
    for (i = 0; i < voices.length; i++) { if ((voices[i].lang || '').toLowerCase() === want) { return voices[i]; } }
    for (i = 0; i < voices.length; i++) { if ((voices[i].lang || '').toLowerCase().indexOf(base) === 0) { return voices[i]; } }
    return null;
  }

  // iOS only allows speech after a user gesture: speak a silent utterance on first tap.
  function unlockSpeech() {
    if (unlocked || !synth) { return; }
    unlocked = true;
    try {
      var u = new window.SpeechSynthesisUtterance(' ');
      u.volume = 0;
      synth.speak(u);
    } catch (e) { /* ignore */ }
  }

  function speak(text) {
    if (!synth) { setStatus(S.noSpeak); return; }
    synth.cancel();
    var u = new window.SpeechSynthesisUtterance(text);
    var v = pickVoice();
    u.lang = v ? v.lang : recLang;
    if (v) { u.voice = v; }
    u.onstart = function () { setStatus(S.speaking); announceSpeech('start'); };
    u.onboundary = function () { announceSpeech('boundary'); };
    u.onend = function () { setStatus(S.ready); announceSpeech('end'); announceState('idle'); };
    u.onerror = function () { setStatus(S.ready); announceSpeech('end'); announceState('idle'); };
    synth.speak(u);
  }

  // ---- send and poll --------------------------------------------------------

  function finishTurn(status, isError) {
    busy = false;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    setStatus(status, isError);
    announceState('idle');
  }

  function handleAuthLost() {
    storeClear();
    busy = false;
    showPairing();
  }

  function sendText(text) {
    text = (text || '').trim();
    if (!text || busy || !token) { return; }
    busy = true;
    $('heard').textContent = text;
    $('reply').textContent = '';
    setStatus(S.sending);
    announceState('thinking');
    fetch('/turn', { method: 'POST', headers: authHeaders(), body: JSON.stringify({ text: text }) })
      .then(function (res) {
        if (res.status === 401) { handleAuthLost(); return null; }
        if (res.status === 429) { finishTurn(S.wait, true); return null; }
        if (res.status === 409) { finishTurn(S.notReady, true); return null; }
        if (res.status === 413 || res.status === 422) { finishTurn(S.tooLong, true); return null; }
        if (!res.ok) { finishTurn(S.error, true); return null; }
        return res.json();
      })
      .then(function (data) {
        if (!data) { return; }
        $('textInput').value = '';
        setStatus(S.waiting);
        pollDeadline = Date.now() + POLL_GIVE_UP_MS;
        poll(data.after);
      })
      .catch(function () { finishTurn(S.error, true); });
  }

  function poll(after) {
    if (Date.now() > pollDeadline) { finishTurn(S.noReply); return; }
    fetch('/reply?after=' + encodeURIComponent(after), { method: 'GET', headers: authHeaders() })
      .then(function (res) {
        if (res.status === 401) { handleAuthLost(); return null; }
        if (!res.ok) { finishTurn(S.error, true); return null; }
        return res.json();
      })
      .then(function (data) {
        if (!data) { return; }
        if (data.status === 'ready') {
          busy = false;
          $('reply').textContent = data.text || '';
          if (data.text) { speak(data.text); } else { setStatus(S.ready); announceState('idle'); }
        } else if (data.status === 'no_reply' || data.status === 'idle') {
          finishTurn(S.noReply);
        } else {
          pollTimer = setTimeout(function () { poll(after); }, POLL_MS);
        }
      })
      .catch(function () { finishTurn(S.error, true); });
  }

  // ---- speech in ------------------------------------------------------------

  var SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;

  function startListening() {
    unlockSpeech();
    if (busy || !token) { return; }
    if (!SpeechRec) { setStatus(S.noSpeech, true); return; }
    if (synth) { synth.cancel(); }
    heardText = '';
    recognition = new SpeechRec();
    recognition.lang = recLang;
    recognition.interimResults = true;
    recognition.continuous = false;
    recognition.onresult = function (ev) {
      var out = '';
      for (var i = 0; i < ev.results.length; i++) { out += ev.results[i][0].transcript; }
      heardText = out;
      $('heard').textContent = out;
    };
    recognition.onerror = function () { setStatus(S.speechFail, true); heardText = ''; };
    recognition.onend = function () {
      $('talk').classList.remove('active');
      recognition = null;
      if (heardText.trim()) { sendText(heardText); } else if (!busy) { setStatus(S.ready); }
    };
    try {
      recognition.start();
      $('talk').classList.add('active');
      setStatus(S.listening);
    } catch (e) { recognition = null; setStatus(S.speechFail, true); }
  }

  function stopListening() { if (recognition) { try { recognition.stop(); } catch (e) { /* ignore */ } } }

  // ---- init -----------------------------------------------------------------

  function labelButton(id, text) {
    var el = $(id);
    el.setAttribute('aria-label', text);
    el.title = text;
  }

  function applyStrings() {
    document.documentElement.lang = isSpanish ? 'es' : 'en';
    $('codeLabel').textContent = S.enterCode;
    $('pairBtn').textContent = S.pair;
    labelButton('talk', S.talk);
    $('heard').setAttribute('aria-label', S.heard);
    $('reply').setAttribute('aria-label', S.reply);
    $('textInput').placeholder = S.textPlaceholder;
    $('textInput').setAttribute('aria-label', S.textPlaceholder);
    labelButton('sendBtn', S.send);
    labelButton('stop', S.stop);
    $('unlink').textContent = '\u2715';
    labelButton('unlink', S.unlink);
    applyAvatarVisibility();
  }

  function init() {
    applyStrings();
    loadVoices();
    if (synth) { synth.onvoiceschanged = loadVoices; }
    document.addEventListener('pointerdown', unlockSpeech, { once: true });

    var talk = $('talk');
    talk.addEventListener('pointerdown', function (e) { e.preventDefault(); startListening(); });
    talk.addEventListener('pointerup', stopListening);
    talk.addEventListener('pointercancel', stopListening);
    talk.addEventListener('pointerleave', stopListening);
    talk.addEventListener('contextmenu', function (e) { e.preventDefault(); });

    $('sendBtn').addEventListener('click', function () { unlockSpeech(); sendText($('textInput').value); });
    $('textInput').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { unlockSpeech(); sendText($('textInput').value); }
    });
    $('stop').addEventListener('click', function () {
      if (synth) { synth.cancel(); }
      setStatus(S.ready);
      announceSpeech('end');
      announceState('idle');
    });
    $('avatarToggle').addEventListener('click', function () {
      avatarHiddenSet(!avatarHiddenGet());
      applyAvatarVisibility();
    });
    $('unlink').addEventListener('click', function () { storeClear(); showPairing(); });
    $('pairBtn').addEventListener('click', function () { pairWith($('codeInput').value); });

    var hashCode = consumeHashCode();
    token = storeGet();
    if (token) {
      showMain();
      if (!SpeechRec) { setStatus(S.noSpeech); }
    } else if (hashCode) {
      showPairing();
      pairWith(hashCode);
    } else {
      showPairing();
    }
  }

  init();
})();
