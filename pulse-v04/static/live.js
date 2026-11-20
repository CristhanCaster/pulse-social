const el = id => document.getElementById(id);
const video = el('video');
let media, audio, meterFrame, room, peer, ice, host, polling;
let busy = false;
const links = new Map();

function status(message) {
  el('status').textContent = message;
}

async function request(path, data) {
  const options = data === undefined ? {} : {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data),
  };
  const response = await fetch('/api/' + path, options);
  const result = await response.json();
  if (!response.ok) throw Object.assign(new Error(result.error), {status: response.status});
  return result;
}

const live = (action, data) => request('live/' + action, data);
const session = (action, data = {}) => live(action, {room: room.id, peer, ...data});
const link = id => new URL('/live?watch=' + id, location.origin).href;

async function preview() {
  if (media) return;
  if (!navigator.mediaDevices?.getUserMedia) throw Error('Camera access requires HTTPS or localhost.');
  media = await navigator.mediaDevices.getUserMedia({
    video: {width: {ideal: 640}, height: {ideal: 360}, frameRate: {ideal: 24}},
    audio: {echoCancellation: true},
  });
  video.muted = true;
  video.hidden = false;
  video.srcObject = media;
  el('preview-tools').hidden = false;
  el('camera').textContent = 'Turn camera off';
  el('mic').textContent = 'Mute mic';
  if (!window.AudioContext) return;
  audio = new AudioContext();
  const analyser = audio.createAnalyser();
  audio.createMediaStreamSource(media).connect(analyser);
  const samples = new Float32Array(analyser.fftSize);
  function meter() {
    analyser.getFloatTimeDomainData(samples);
    const energy = samples.reduce((sum, sample) => sum + sample * sample, 0);
    el('level').value = Math.min(1, 4 * Math.sqrt(energy / samples.length));
    meterFrame = requestAnimationFrame(meter);
  }
  audio.resume().catch(() => {});
  meter();
}

function stopPreview() {
  cancelAnimationFrame(meterFrame);
  audio?.close().catch(() => {});
  audio = null;
  media?.getTracks().forEach(track => track.stop());
  media = null;
  video.srcObject = null;
  video.hidden = true;
  el('preview-tools').hidden = true;
  el('level').value = 0;
}

function toggleTrack(kind, button, enabledLabel, disabledLabel) {
  const track = media?.getTracks().find(item => item.kind === kind);
  if (!track) return;
  track.enabled = !track.enabled;
  el(button).textContent = track.enabled ? enabledLabel : disabledLabel;
}

async function refresh() {
  const {rooms} = await live('list');
  if (room) return;
  el('streams').replaceChildren(...rooms.map(item => {
    const row = document.createElement('li');
    const anchor = document.createElement('a');
    anchor.href = link(item.id);
    anchor.textContent = item.title + ' · @' + item.username;
    row.append(anchor);
    return row;
  }));
  if (!rooms.length) {
    const row = document.createElement('li');
    row.textContent = 'No broadcasts available.';
    el('streams').append(row);
  }
}

function enter(result, broadcasting) {
  room = result.room;
  peer = result.peer;
  ice = result.ice;
  host = broadcasting;
  el('studio-title').textContent = room.title + ' · @' + room.username;
  el('broadcast').hidden = true;
  el('discovery').hidden = true;
  el('session').hidden = false;
  el('chat').hidden = false;
  el('cancel').hidden = host;
  el('leave').textContent = host ? 'End broadcast' : 'Leave stream';
  video.hidden = false;
  video.muted = true;
  video.controls = !host;
  history.replaceState(null, '', link(room.id));
  status(host ? 'You’re live.' : 'Connecting to the broadcast…');
  poll(room);
}

function reset() {
  clearTimeout(polling);
  for (const connection of links.values()) connection.close();
  links.clear();
  stopPreview();
  room = peer = ice = null;
  host = false;
  el('studio-title').textContent = 'Broadcast studio';
  el('broadcast').hidden = false;
  el('discovery').hidden = false;
  el('session').hidden = true;
  el('chat').hidden = true;
  el('cancel').hidden = false;
  el('chat-log').replaceChildren();
  el('message').value = '';
  el('send').disabled = false;
  history.replaceState(null, '', '/live');
  refresh().catch(error => status(error.message));
}

function makeConnection(id, roomAtStart) {
  const connection = new RTCPeerConnection({iceServers: ice});
  links.set(id, connection);
  connection.addEventListener('connectionstatechange', () => {
    if (room !== roomAtStart || host) return;
    if (connection.connectionState === 'connected') status('Tap the speaker on the player to turn on sound.');
    if (connection.connectionState === 'failed') status('Video could not connect. Leave and reopen the stream to retry.');
  });
  return connection;
}

async function description(connection, type) {
  const initial = type === 'offer' ? await connection.createOffer() : await connection.createAnswer();
  await connection.setLocalDescription(initial);
  if (connection.iceGatheringState !== 'complete') {
    await new Promise(resolve => {
      function finish() {
        clearTimeout(timer);
        connection.removeEventListener('icegatheringstatechange', changed);
        resolve();
      }
      function changed() {
        if (connection.iceGatheringState === 'complete') finish();
      }
      const timer = setTimeout(finish, 4500);
      connection.addEventListener('icegatheringstatechange', changed);
      changed();
    });
  }
  return connection.localDescription?.sdp;
}

async function offer(entry, current) {
  const connection = makeConnection(entry.id, current);
  try {
    media.getTracks().forEach(track => connection.addTrack(track, media));
    const sdp = await description(connection, 'offer');
    if (room === current && links.get(entry.id) === connection) {
      await session('offer', {peer: entry.id, sdp});
    }
  } catch {
    connection.close();
    if (links.get(entry.id) === connection) links.delete(entry.id);
  }
}

async function answer(sdp, current) {
  const connection = makeConnection(peer, current);
  try {
    connection.ontrack = event => { if (room === current) video.srcObject = event.streams[0]; };
    await connection.setRemoteDescription({type: 'offer', sdp});
    const reply = await description(connection, 'answer');
    if (room === current) await session('answer', {sdp: reply});
  } catch (error) {
    connection.close();
    for (const [id, item] of links) if (item === connection) links.delete(id);
    if (room === current) status(error.message);
  }
}

function drawChat(messages, muted) {
  el('send').disabled = muted;
  el('message').placeholder = muted ? 'Muted by the streamer' : 'Say something';
  const log = el('chat-log');
  const key = JSON.stringify(messages);
  if (log.dataset.key === key && (messages.length === 0 || log.childElementCount)) return;
  log.dataset.key = key;
  const nearBottom = log.scrollHeight - log.clientHeight - log.scrollTop < 40;
  log.replaceChildren(...messages.map(message => {
    const row = document.createElement('div');
    row.className = 'message';
    row.textContent = '@' + message.username + ' · ' + message.body;
    if (host) {
      for (const [action, label] of [['delete', 'Delete message'], ['mute', 'Mute in chat']]) {
        if (action === 'mute' && message.user === room.host) continue;
        const button = document.createElement('button');
        button.textContent = label;
        button.onclick = () => session(action, {message: message.id})
          .then(() => { if (action === 'mute') status('@' + message.username + ' is muted in chat.'); })
          .catch(error => status(error.message));
        row.append(button);
      }
    }
    return row;
  }));
  if (nearBottom) log.scrollTop = log.scrollHeight;
}

async function poll(current) {
  try {
    const {signals, chat, muted} = await session('poll');
    if (room !== current) return;
    drawChat(chat, muted);
    if (host) {
      const active = new Set(signals.map(entry => entry.id));
      for (const [id, pc] of links) {
        if (!active.has(id)) {
          pc.close();
          links.delete(id);
        }
      }
      for (const entry of signals) {
        const pc = links.get(entry.id);
        if (!pc) void offer(entry, current);
        else if (entry.answer && pc.signalingState === 'have-local-offer') {
          try {
            await pc.setRemoteDescription({type: 'answer', sdp: entry.answer});
          } catch {
            pc.close();
          }
        }
      }
    } else if (signals && !links.has(peer)) void answer(signals, current);
  } catch (error) {
    if (room !== current) return;
    if ([401, 403, 404, 409].includes(error.status)) reset();
    status(error.message);
  } finally {
    if (room === current) polling = setTimeout(() => poll(current), 1300);
  }
}

function run(fn) {
  return async event => {
    event?.preventDefault();
    if (busy) return;
    busy = true;
    try { await fn(); }
    catch (error) { status(error.message); }
    finally { busy = false; }
  };
}

el('preview').onclick = run(preview);
el('cancel').onclick = run(stopPreview);
el('camera').onclick = () => toggleTrack('video', 'camera', 'Turn camera off', 'Turn camera on');
el('mic').onclick = () => toggleTrack('audio', 'mic', 'Mute mic', 'Unmute mic');
el('broadcast').onsubmit = run(async () => {
  await preview();
  enter(await live('start', {title: el('title').value, visibility: el('audience').value}), true);
});
el('leave').onclick = run(async () => {
  await session(host ? 'end' : 'leave');
  reset();
  status('Stream closed.');
});
el('share').onclick = run(async () => {
  await navigator.clipboard.writeText(link(room.id));
  status('Stream link copied.');
});
el('chat-form').onsubmit = run(async () => {
  await session('chat', {body: el('message').value});
  el('message').value = '';
});
window.addEventListener('pagehide', () => {
  if (room) {
    fetch('/api/live/' + (host ? 'end' : 'leave'), {
      method: 'POST', keepalive: true, headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({room: room.id, peer}),
    }).catch(() => {});
  }
  if (room || media) {
    clearTimeout(polling);
    for (const pc of links.values()) pc.close();
    stopPreview();
  }
});
window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });

async function init() {
  const {user} = await request('me');
  if (!user) {
    status('Log in to Pulse to use Live. ');
    const login = document.createElement('a');
    login.textContent = 'Log in';
    login.href = '/?returnTo=' + encodeURIComponent(location.pathname + location.search);
    el('status').append(login);
    return;
  }
  el('layout').hidden = false;
  const watching = new URLSearchParams(location.search).get('watch');
  if (watching) {
    try { enter(await live('join', {room: watching}), false); }
    catch (error) { status(error.message); }
  }
  if (!room) await refresh();
  setInterval(() => { if (!room) refresh().catch(() => {}); }, 8000);
}

init().catch(error => status(error.message));
