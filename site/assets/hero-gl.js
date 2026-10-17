/* Hero background: a procedural ribbon of light drawn by a WebGL1 fragment shader. No video, no image, no library.
   - Perfect loop: every time-dependent term is sin/cos of an integer multiple of the phase (period T = 12 s);
     the grain is re-seeded from an integer frame index in [0, N), so frame N equals frame 0.
   - Black without WebGL (the canvas stays transparent over the black page); still frame with prefers-reduced-motion.
   - Cap of 30 fps, paused when the tab is hidden or the hero is off screen (or hidden by the light theme).
   - DPR capped at 2, and the render scale drops when a frame costs more than ~20 ms. */
(function () {
  var canvas = document.getElementById('hero-gl');
  if (!canvas) return;

  var T = 12;                 // loop period, seconds
  var FPS = 30;               // frame cap
  var STEPS = T * FPS;        // grain frames per loop
  var STATIC_PHASE = 0.14;    // fraction of the loop shown when motion is reduced
  var SLOW_MS = 20;           // above this median frame cost the resolution scale drops
  var FAST_MS = 8;            // below this the scale may grow back toward 1
  var MIN_SCALE = 0.5;
  var WARM = 4, WINDOW = 12, IDLE = 90;   // frames: skipped, sampled, left alone

  var VERT = 'attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}';
  var FRAG = [
    '#ifdef GL_FRAGMENT_PRECISION_HIGH',
    'precision highp float;',
    '#else',
    'precision mediump float;',
    '#endif',
    'uniform vec2 uRes;uniform float uPh;uniform float uK;uniform float uFree;',
    'const float TAU=6.2831853;',
    'float hash(vec2 q){q=fract(q*vec2(.1031,.1030));q+=dot(q,q.yx+33.33);return fract((q.x+q.y)*q.x);}',
    /* One twisting strip. c: centre line, th: twist angle, hw: half height. Returns light (0..1). */
    'float ribbon(float s,float t,float free,float c,float th,float h,float sheen){',
    '  float ca=cos(th);',
    '  float hw=h*abs(ca)+.012;',                       // edge-on: a thin bright thread, never zero
    '  float v=(t-c)/hw;',
    '  float av=abs(v);',
    '  float ndv=clamp(abs(ca)*(1.-.35*v*v),0.,1.);',    // surface normal against the view direction
    '  float fres=pow(1.-ndv,2.2);',                     // fresnel: translucent face-on, bright edge-on
    '  float soft=.16+.34*(1.-abs(ca));',                // blur grows as the strip turns away
    '  float cov=1.-smoothstep(1.-soft,1.+soft*.5,av);',
    '  float body=cov*(.13+.78*fres)*(1.+sheen*sin(v*2.6+th*2.));',
    '  float rim=cov*pow(av,5.)*.34;',                   // soft glow on the borders
    '  float halo=.16*exp(-max(av-1.,0.)*hw*free*70.)*(1.-cov*.6);',
    '  return clamp(body+rim+halo*step(1.,av+.0001),0.,1.);',
    '}',
    'void main(){',
    '  vec2 uv=gl_FragCoord.xy/uRes;',
    '  float s=uv.x-.5;',
    '  float a=uRes.x/uRes.y;',
    /* The ribbon lives in the free band above the hero text (uFree = its height as a share of the canvas),
       so it never sits behind the lines of text. tt runs 0..1 inside that band. */
    '  float tt=(uv.y-(1.-uFree))/uFree;',
    '  float ph=uPh;',
    '  float c1=.56+.12*sin(4.6*s+ph)+.04*sin(10.5*s-2.*ph+1.);',
    '  float th1=6.2*s-ph+.7*sin(2.8*s+ph);',
    '  float c2=.60+.10*sin(4.1*s-ph+2.2)+.035*sin(9.*s+2.*ph+.4);',
    '  float th2=5.4*s+ph+1.9+.8*sin(3.3*s-ph+.6);',
    '  float l1=ribbon(s,tt,uFree,c1,th1,.34,.10);',
    '  float l2=ribbon(s,tt,uFree,c2,th2,.27,.08)*.72;',
    '  float lum=1.-(1.-l1)*(1.-l2);',
    '  lum*=smoothstep(.0,.1,tt)*.86;',
    /* wide screens: the faint terminal sits at the top right, so the light fades out before it */
    '  lum*=1.-.82*smoothstep(.10,.24,s)*smoothstep(1.5,1.8,a);',
    '  float g=hash(gl_FragCoord.xy+uK*vec2(37.,17.))-.5;',
    '  lum+=g*.085*sqrt(lum);',                           // grain rides the light, black stays pure black
    '  lum=max(lum,0.);',
    '  gl_FragColor=vec4(vec3(.16,.72,.44)*lum,1.);',
    '}'
  ].join('\n');

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  var gl = null, prog = null, loc = {}, buf = null;
  var onScreen = false, running = false, raf = 0;
  var scale = 1, last = 0, elapsed = STATIC_PHASE * T * 1000, frames = 0, samples = [], lost = false;

  function compile(type, src) {
    var sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) return null;
    return sh;
  }

  function setup() {
    try {
      gl = canvas.getContext('webgl', { alpha: false, antialias: false, depth: false, stencil: false,
        powerPreference: 'low-power', preserveDrawingBuffer: false }) ||
        canvas.getContext('experimental-webgl');
    } catch (e) { gl = null; }
    if (!gl) return false;
    var vs = compile(gl.VERTEX_SHADER, VERT), fs = compile(gl.FRAGMENT_SHADER, FRAG);
    if (!vs || !fs) return false;
    prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return false;
    gl.useProgram(prog);
    buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    var p = gl.getAttribLocation(prog, 'p');
    gl.enableVertexAttribArray(p);
    gl.vertexAttribPointer(p, 2, gl.FLOAT, false, 0, 0);
    loc.res = gl.getUniformLocation(prog, 'uRes');
    loc.ph = gl.getUniformLocation(prog, 'uPh');
    loc.k = gl.getUniformLocation(prog, 'uK');
    loc.free = gl.getUniformLocation(prog, 'uFree');
    return true;
  }

  function resize() {
    var dpr = Math.min(window.devicePixelRatio || 1, 2) * scale;
    var w = Math.max(1, Math.round(canvas.clientWidth * dpr));
    var h = Math.max(1, Math.round(canvas.clientHeight * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    gl.viewport(0, 0, w, h);
  }

  /* frac: position in the loop, 0 <= frac < 1 */
  function draw(frac) {
    resize();
    gl.uniform2f(loc.res, canvas.width, canvas.height);
    // free band above the hero text (it runs 30 px behind the opaque badge), as a share of the canvas height (at least 22%)
    var inner = canvas.parentNode.querySelector('.hero-in');
    var free = inner && canvas.clientHeight ? (inner.offsetTop + 30) / canvas.clientHeight : 0.4;
    gl.uniform1f(loc.free, Math.min(0.9, Math.max(0.22, free)));
    gl.uniform1f(loc.ph, 6.2831853 * frac);
    gl.uniform1f(loc.k, Math.floor(frac * STEPS) % STEPS);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  function shown() { canvas.classList.add('on'); }

  /* Resolution scale: sample the real GPU cost (gl.finish) on a window of frames, skipping the warm-up ones, and use
     the median. Above SLOW_MS render smaller; well under FAST_MS (with room to grow back) render bigger again. */
  function adapt(a) {
    var ph = frames++ % (WARM + WINDOW + IDLE);
    if (ph < WARM) return;
    if (ph < WARM + WINDOW) {
      gl.finish();
      samples.push(performance.now() - a);
      return;
    }
    if (ph === WARM + WINDOW) {
      samples.sort(function (x, y) { return x - y; });
      var med = samples[samples.length >> 1];
      samples = [];
      if (med > SLOW_MS && scale > MIN_SCALE) scale = Math.max(MIN_SCALE, scale * 0.75);
      else if (med < FAST_MS && scale < 1) scale = Math.min(1, scale / 0.75);
    }
  }

  function tick(now) {
    raf = 0;
    if (!running) return;
    raf = requestAnimationFrame(tick);
    if (now - last < 1000 / FPS - 2) return;           // cap at 30 fps
    // The loop clock only advances while frames run, so pausing and resuming never makes the picture jump.
    if (last) elapsed += Math.min(now - last, 100);
    last = now;
    var a = performance.now();
    draw(phase());
    shown();
    adapt(a);
  }

  function phase() { return ((elapsed / 1000) % T) / T; }

  function stop() {
    running = false;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
  }

  function start() {
    if (running || !gl || lost) return;
    running = true;
    last = 0;                                          // elapsed is kept: resume where it stopped
    raf = requestAnimationFrame(tick);
  }

  function still() {
    if (!gl || lost) return;
    draw(STATIC_PHASE);
    shown();
  }

  function update() {
    if (!gl || lost) return;
    var live = onScreen && !document.hidden;
    if (reduce.matches) {
      stop();
      if (onScreen) still();
    } else if (live) {
      start();
    } else {
      stop();
    }
  }

  if (!setup()) { canvas.removeAttribute('data-gl'); return; }
  canvas.setAttribute('data-gl', 'on');

  canvas.addEventListener('webglcontextlost', function (e) {
    e.preventDefault();
    lost = true;
    stop();
    canvas.classList.remove('on');
  });
  canvas.addEventListener('webglcontextrestored', function () {
    lost = false;
    if (setup()) update();
  });

  // Handle for tests and debugging: draw any point of the loop, read the loop position.
  canvas.heroGL = { render: function (frac) { draw(frac); }, phase: phase };

  document.addEventListener('visibilitychange', update);
  if (reduce.addEventListener) reduce.addEventListener('change', update);
  window.addEventListener('resize', function () { if (reduce.matches && onScreen) still(); });

  if ('IntersectionObserver' in window) {
    // Also false while the canvas is display:none (light theme), so nothing renders there.
    new IntersectionObserver(function (es) {
      onScreen = es[es.length - 1].isIntersecting;
      update();
    }).observe(canvas);
  } else {
    onScreen = true;
    update();
  }
})();
