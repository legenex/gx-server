// Shared realtime camera capture: a live preview plus JPEG stills at a small
// frame rate, which is what an omni model consumes (gx-live.v1 allows two per
// second, 512 KiB each). Frames are encoded from a canvas and handed to the
// caller; nothing is stored and no video is ever recorded.
const MAX_SIDE = 768; // plenty for the vision encoder, and kind to the fabric

export async function startCamera({ video, onFrame, fps = 1, facingMode = 'user', maxSide = MAX_SIDE } = {}) {
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { facingMode, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false,
  });
  if (video) {
    video.srcObject = stream;
    video.muted = true;
    video.playsInline = true;
    try { await video.play(); } catch { /* the preview may autoplay later */ }
  }
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  let timer = null;
  let stopped = false;
  let frames = 0;

  const grab = () => new Promise((resolve) => {
    const source = video && video.videoWidth ? video : null;
    if (!source) { resolve(null); return; }
    const scale = Math.min(1, maxSide / Math.max(source.videoWidth, source.videoHeight));
    canvas.width = Math.max(2, Math.round(source.videoWidth * scale));
    canvas.height = Math.max(2, Math.round(source.videoHeight * scale));
    ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((blob) => resolve(blob), 'image/jpeg', 0.7);
  });

  const tick = async () => {
    if (stopped) return;
    const blob = await grab();
    if (blob && !stopped && onFrame) {
      const buffer = await blob.arrayBuffer();
      if (!stopped) { frames += 1; onFrame(new Uint8Array(buffer)); }
    }
  };
  timer = setInterval(tick, Math.max(500, Math.round(1000 / Math.max(0.2, fps))));
  tick();

  return {
    stream,
    get frames() { return frames; },
    /** One extra frame right now (used when a typed message is sent). */
    capture: grab,
    stop() {
      if (stopped) return;
      stopped = true;
      if (timer) clearInterval(timer);
      stream.getTracks().forEach((t) => t.stop());
      if (video) video.srcObject = null;
    },
  };
}
