/* The GEMP wordmark, inline.
 *
 * Inline rather than <img src="logo.svg"> for one reason that matters: an <img> is an
 * opaque document, so page CSS cannot reach inside it. The mark is navy on light
 * green, and navy on the dark appearance's #12161c ground is very nearly invisible -
 * the letters would simply disappear for half the users. Inlined, the fills are
 * classes and each appearance picks its own.
 *
 * The geometry is the supplied artwork untouched. Only the viewBox changed: the
 * original is a 2048 square around an 816x185 mark, so it drew at a twelfth of the
 * size it should in any box that was not square.
 *
 * The file lives here for the same reason the charts and icons do - the
 * Content-Security-Policy is `default-src 'self'`, so nothing is fetched from
 * anywhere, and F13 requires the demonstration to survive an unplugged cable.
 */

'use strict';

const VIEWBOX = '575 931 832 201';
const PATHS = '<path class="wm-accent" d="M659.725 1003.86C663.322 993.469 667.406 984.911 674.029 976.021A91.43 91.43 0 0 1 734.943 940.444C768.255 935.822 793.55 944.132 819.707 963.81A1393 1393 0 0 0 794.848 993.873C765.486 968.559 725.071 966.923 703.427 1003.67Q757.242 1004.015 811.058 1003.79C822.05 1003.78 852.921 1003.08 862.691 1004.33C864.692 999.919 863.526 951.317 863.679 942.215C907.444 942.231 954.941 941.32 998.395 942.335L998.421 977.484L902.919 977.593L903.017 1003.9C929.29 1003.8 959.605 1003.07 985.611 1003.96C987.512 1020.42 987.521 1040.58 986.813 1057.24L985.032 1058.86C968.362 1058.5 940.418 1061.49 924.623 1059.08C918.409 1059.55 909.376 1059.37 902.936 1059.42L902.984 1086.47L1000 1086.44L999.971 1121.21L863.735 1121.21C863.34 1100.76 863.643 1079.65 863.685 1059.15L854.298 1059.05C845.475 1059.43 834.052 1059 825.939 1059.58L825.946 1095.91C771.369 1142.66 683.573 1133.68 659.148 1059.21C653.877 1040.52 654.802 1022.42 659.725 1003.86ZM702.921 1059.73C721.976 1093.1 757.555 1095.79 788.056 1077.99Q787.993 1068.63 788.207 1059.27C759.713 1059.95 731.307 1058.94 702.921 1059.73Z"/><path class="wm-ink" d="M659.725 1003.86C663.322 993.469 667.406 984.911 674.029 976.021A91.43 91.43 0 0 1 734.943 940.444C768.255 935.822 793.55 944.132 819.707 963.81A1393 1393 0 0 0 794.848 993.873C765.486 968.559 725.071 966.923 703.427 1003.67C695.915 1019.86 693.408 1043.73 702.921 1059.73C721.976 1093.1 757.555 1095.79 788.056 1077.99Q787.993 1068.63 788.207 1059.27C787.919 1055.64 788.521 1054.82 786.638 1052.56C777.333 1051.39 758.535 1052.21 748.267 1052.2L748.248 1018.21L826.007 1018.21L825.939 1059.58L825.946 1095.91C771.369 1142.66 683.573 1133.68 659.148 1059.21C653.877 1040.52 654.802 1022.42 659.725 1003.86ZM1035.96 942.446C1049.78 942.379 1063.6 942.429 1077.41 942.594L1124.44 1017.96L1171.39 942.581C1185.55 942.371 1199.72 942.379 1213.89 942.607L1213.84 1121.14C1200.91 1121.02 1187.83 1121.16 1174.88 1121.2L1174.89 1044.5L1174.75 1004.73C1157.88 1028.34 1140.12 1057 1123.82 1081.47A6922 6922 0 0 0 1073.8 1005.75C1073.12 1043.94 1073.69 1082.84 1073.44 1121.14L1035.07 1121.18C1035.06 1103.44 1034.17 946.325 1035.96 942.446ZM1256.8 942.454C1287.75 942.361 1345.34 937.566 1370.73 952.84A55.2 55.2 0 0 1 1396.24 987.441C1406.08 1027.21 1383.97 1057.12 1344.91 1065.71C1330.46 1068.38 1310.92 1067.69 1295.95 1067.65L1295.97 1121.16C1282.92 1121.1 1269.87 1121.11 1256.83 1121.2ZM1295.98 1032.24C1313.21 1032.45 1338.51 1035.83 1350.79 1024.17C1357.6 1016.05 1359.09 1011.26 1358.09 1000.48C1355.46 972.228 1315.81 977.652 1295.97 977.923Z"/><path class="wm-accent" d="M659.725 1003.86C654.802 1022.42 653.877 1040.52 659.148 1059.21C648.684 1059.59 592.88 1060.13 585.151 1058.35C580.939 1051.86 583.027 1012.8 584.447 1004.06C589.6 1005.13 603.794 1004.03 610.093 1003.96C626.637 1003.8 643.181 1003.77 659.725 1003.86Z"/>';

/* `title` is what a screen reader announces. The mark spells the name, so the
 * pages that use it drop their duplicate text heading rather than saying it
 * twice.
 *
 * There is no cropped variant any more. It existed for a 64px collapsed rail;
 * the rail is 5.25rem at every width now and the full wordmark fits. */
/* The sign-in scene.
 *
 * Drawn, not fetched. The Content-Security-Policy is `default-src 'self'` and F13
 * requires the demonstration to survive an unplugged cable, so a rendered
 * illustration from a CDN is not available even if it were wanted - and an
 * inlined bitmap would neither re-colour with the appearance nor stay sharp on a
 * projector. This is geometry, so it does both.
 *
 * What it draws is the product's own subject: a district of public buildings seen
 * in isometric, some of them fitted with rooftop solar, a couple of turbines on
 * the skyline. The buildings that carry a retrofit are picked out in the brand
 * mint, which is the same two-state language the allocation map uses for funded
 * and not funded.
 *
 * It carries no data and never changes. The heights, the street gaps and which
 * roofs get panels all come from a fixed table, identical on every load and for
 * every visitor. A sign-in screen must not describe a portfolio to somebody who
 * has not signed in, and a scene that looked like real figures would be doing
 * exactly that.
 *
 * `aria-hidden`: the sentence beside it carries the meaning.
 */

/* Half-width and half-depth of one ground tile. 34:19 is close to a true 2:1
 * isometric once the stroke is allowed for. */
const TILE_W = 34;
const TILE_H = 19;

/* The district. `0` is a street, any other number is a building of that height in
 * scene units. `s` marks the roofs that carry solar. Fixed, so the mark is a mark
 * rather than a slot machine. */
const BLOCKS = [
  [58, 40, 74, 0, 66, 46, 34],
  [36, 82, 50, 0, 44, 92, 58],
  [70, 44, 0, 0, 0, 38, 76],
  [0, 0, 0, 54, 0, 0, 0],
  [64, 38, 0, 0, 0, 70, 42],
  [46, 88, 56, 0, 78, 50, 66],
  [34, 52, 68, 0, 40, 60, 44],
];
const SOLAR = new Set([
  '0,0', '0,2', '0,5', '1,1', '1,5', '2,0', '2,6',
  '4,0', '4,5', '5,1', '5,4', '5,6', '6,2', '6,5',
]);

function isoBox(gx, gy, h, solar) {
  const cx = (gx - gy) * TILE_W;
  const cy = (gx + gy) * TILE_H;
  const top = `${cx},${cy - h} ${cx + TILE_W},${cy + TILE_H - h} `
            + `${cx},${cy + 2 * TILE_H - h} ${cx - TILE_W},${cy + TILE_H - h}`;
  const left = `${cx - TILE_W},${cy + TILE_H - h} ${cx},${cy + 2 * TILE_H - h} `
             + `${cx},${cy + 2 * TILE_H} ${cx - TILE_W},${cy + TILE_H}`;
  const right = `${cx},${cy + 2 * TILE_H - h} ${cx + TILE_W},${cy + TILE_H - h} `
              + `${cx + TILE_W},${cy + TILE_H} ${cx},${cy + 2 * TILE_H}`;

  /* The panel is a smaller rhombus lying on the roof, with two rafters across it
   * so it reads as an array rather than a coloured lid. */
  let panel = '';
  if (solar) {
    const k = 0.62;
    const px = cx;
    const py = cy + TILE_H - h;
    panel = `<polygon class="iso-solar" points="${px},${py - TILE_H * k} `
          + `${px + TILE_W * k},${py} ${px},${py + TILE_H * k} ${px - TILE_W * k},${py}"/>`
          + `<path class="iso-solar-rib" d="M${px - TILE_W * k * 0.5},${py - TILE_H * k * 0.5}`
          + `L${px + TILE_W * k * 0.5},${py + TILE_H * k * 0.5}`
          + `M${px - TILE_W * k * 0.5},${py + TILE_H * k * 0.5}`
          + `L${px + TILE_W * k * 0.5},${py - TILE_H * k * 0.5}"/>`;
  }

  return `<polygon class="iso-left" points="${left}"/>`
       + `<polygon class="iso-right" points="${right}"/>`
       + `<polygon class="iso-top${solar ? ' lit' : ''}" points="${top}"/>`
       + panel;
}

function turbine(cx, cy, scale, variant) {
  const H = 96 * scale;
  const R = 30 * scale;
  const pad = R + 6;

  /* The rotor lives in its own nested <svg> whose centre is the hub, so the
   * group can be positioned without a `transform` - a CSS animation on
   * `transform` would overwrite a transform attribute, and an inline style is
   * dropped by this project's Content-Security-Policy.
   *
   * What turns the rotor is `transform-origin`, and getting that wrong is why
   * the blades used to orbit a point below and to the right of the mast instead
   * of turning on it. `transform-box: view-box` resolves against a viewport that
   * is not the one this markup establishes, so the origin landed 55px away from
   * the hub. `fill-box` resolves against the group's own bounding box, which is
   * a box this code controls: the invisible circle below is centred on the hub
   * and reaches as far as the blades do, so the bounding box is symmetric about
   * the hub whatever the blades are doing. Then `transform-origin: center` can
   * only mean the hub. */
  const rot = (a, x, y) => {
    const r = a * Math.PI / 180;
    return `${(x * Math.cos(r) - y * Math.sin(r)).toFixed(1)},`
         + `${(x * Math.sin(r) + y * Math.cos(r)).toFixed(1)}`;
  };

  /* A real blade is wide at the root and narrow at the tip. Three lines of equal
   * weight read as a peace sign; the taper is what reads as a turbine. */
  const w = 3.6 * scale;
  const blades = [90, 210, 330].map((a) => {
    const pts = [
      rot(a, -w, 0.06 * R),
      rot(a, -w * 0.3, R),
      rot(a, w * 0.3, R),
      rot(a, w, 0.06 * R),
    ].join(' ');
    return `<polygon class="iso-blade" points="${pts}"/>`;
  }).join('');

  const cls = `iso-rotor${variant ? ` iso-rotor-${variant}` : ''}`;

  return `<line class="iso-mast" x1="${cx}" y1="${cy}" x2="${cx}" y2="${cy - H}"/>`
    + `<svg class="iso-rotor-box" x="${(cx - pad).toFixed(1)}" y="${(cy - H - pad).toFixed(1)}"
         width="${(pad * 2).toFixed(1)}" height="${(pad * 2).toFixed(1)}"
         viewBox="${-pad} ${-pad} ${pad * 2} ${pad * 2}" overflow="visible">
        <g class="${cls}">
          <circle class="iso-rotor-bounds" cx="0" cy="0" r="${R.toFixed(1)}"/>
          ${blades}
        </g>
        <circle class="iso-hub" cx="0" cy="0" r="${(3.2 * scale).toFixed(1)}"/>
      </svg>`;
}

export function signInArtwork() {
  const n = BLOCKS.length;
  const drawn = [];
  for (let gy = 0; gy < n; gy += 1) {
    for (let gx = 0; gx < BLOCKS[gy].length; gx += 1) {
      const h = BLOCKS[gy][gx];
      if (!h) continue;
      /* Painter's algorithm: further from the camera first, so a near block
       * overlaps the one behind it rather than the other way round. */
      drawn.push({ order: gx + gy, svg: isoBox(gx, gy, h, SOLAR.has(`${gy},${gx}`)) });
    }
  }
  drawn.sort((a, b) => a.order - b.order);

  /* The ground the district stands on, one tile larger on every side. */
  const half = (n - 1) / 2;
  const gw = (n + 1) * TILE_W;
  const gh = (n + 1) * TILE_H;
  const gcx = 0;
  const gcy = (n - 1) * TILE_H;
  const plate = `<polygon class="iso-plate" points="${gcx},${gcy - gh + TILE_H} `
              + `${gcx + gw},${gcy + TILE_H} ${gcx},${gcy + gh + TILE_H} ${gcx - gw},${gcy + TILE_H}"/>`;

  return `<svg class="scene-svg" viewBox="-250 -120 500 430"
       preserveAspectRatio="xMidYMid meet" aria-hidden="true" focusable="false">
    <defs>
      <radialGradient id="scene-glow" cx="50%" cy="38%" r="62%">
        <stop offset="0%" class="scene-glow-in"/>
        <stop offset="100%" class="scene-glow-out"/>
      </radialGradient>
    </defs>
    <rect x="-250" y="-120" width="500" height="430" fill="url(#scene-glow)"/>
    <g class="iso-scene">
      ${plate}
      ${turbine(-158, 62, 1, 'a')}
      ${turbine(-112, 30, 0.72, 'b')}
      ${drawn.map((d) => d.svg).join('')}
    </g>
  </svg>`;
}

export function wordmark(title = 'GEMP') {
  return `<svg class="wordmark-svg" viewBox="${VIEWBOX}" role="img"
    aria-label="${title}" focusable="false">${PATHS}</svg>`;
}


/* A small drawn figure for each stage of the landing page.
 *
 * Four diagrams rather than four icons: an icon says "there is a topic here",
 * and a diagram says what the topic IS. They share a 132x92 box, one stroke
 * weight and one accent, so four of them down a page read as a set rather than
 * as clip art. Drawn for the same reason everything else here is - the CSP
 * fetches nothing, and geometry re-colours with the appearance where a bitmap
 * would not.
 */
const FIGURES = {
  /* Blocks joined by links, the last one carrying the break the chain walk
     reports. */
  measures: `
    <g class="fig-stroke">
      ${[0, 1, 2, 3].map((i) => `<rect x="${6 + i * 32}" y="34" width="22" height="24" rx="3"/>`).join('')}
      ${[0, 1, 2].map((i) => `<path d="M${28 + i * 32} 46h6"/>`).join('')}
    </g>
    <g class="fig-accent">
      ${[0, 1, 2].map((i) => `<path d="M${11 + i * 32} 46l4 4 7 -8"/>`).join('')}
    </g>
    <path class="fig-warn" d="M105 40l6 10h-12Z"/>
    <path class="fig-muted" d="M6 70h120"/>`,

  /* Two lines over the same hours: what the meter recorded, and what the model
     expected. */
  forecasts: `
    <path class="fig-muted" d="M8 68h116M8 20v48"/>
    <path class="fig-stroke fig-line"
      d="M10 58 L26 40 L42 46 L58 24 L74 36 L90 22 L106 34 L122 26"/>
    <path class="fig-accent fig-line fig-dash"
      d="M10 55 L26 43 L42 44 L58 28 L74 33 L90 26 L106 31 L122 29"/>`,

  /* A budget line, and the bars that fit under it. */
  decides: `
    <path class="fig-muted" d="M8 70h116"/>
    <g class="fig-stroke">
      ${[38, 22, 52, 30, 44, 26, 48].map((h, i) =>
        `<rect x="${10 + i * 17}" y="${70 - h}" width="11" height="${h}" rx="2"/>`).join('')}
    </g>
    <g class="fig-accent-fill">
      ${[[0, 38], [2, 52], [4, 44], [6, 48]].map(([i, h]) =>
        `<rect x="${10 + i * 17}" y="${70 - h}" width="11" height="${h}" rx="2"/>`).join('')}
    </g>
    <path class="fig-warn fig-dash" d="M6 26h122"/>`,

  /* A sheet with a hash across it and a seal. */
  accounts: `
    <rect class="fig-stroke" x="24" y="12" width="70" height="64" rx="4"/>
    <g class="fig-muted">
      <path d="M34 28h40M34 38h50M34 48h34"/>
    </g>
    <path class="fig-accent fig-mono" d="M34 60h30"/>
    <circle class="fig-accent-fill" cx="100" cy="62" r="13"/>
    <path class="fig-seal" d="M94 62l4.5 4.5L107 57"/>`,
};

/* Four more, for "how it is built". Same box, same stroke weight, same accent as
   the stage figures - the page has one drawing language, not two. */
const BUILT = {
  /* A browser and a server talking to each other, and the cloud between them
     struck out: nothing on this platform is fetched from anywhere. */
  offline: `
    <rect class="fig-stroke" x="8" y="26" width="42" height="32" rx="4"/>
    <path class="fig-muted" d="M8 36h42"/>
    <rect class="fig-stroke" x="82" y="26" width="42" height="32" rx="4"/>
    <path class="fig-muted" d="M90 36h26M90 44h18"/>
    <path class="fig-accent fig-flow" d="M50 42h32"/>
    <g class="fig-cloud">
      <path class="fig-muted" d="M52 14a9 9 0 0 1 17-3 8 8 0 0 1 11 8 7 7 0 0 1-7 7H58a7 7 0 0 1-6-12z"/>
      <path class="fig-strike" d="M48 6l38 30"/>
    </g>
    <path class="fig-muted" d="M8 72h116"/>`,

  /* Three rungs, and the refused row that is kept rather than dropped. */
  roles: `
    <g class="fig-stroke">
      <rect x="10" y="14" width="60" height="15" rx="4"/>
      <rect x="10" y="36" width="76" height="15" rx="4"/>
      <rect x="10" y="58" width="92" height="15" rx="4"/>
    </g>
    <g class="fig-accent-fill">
      <circle cx="20" cy="21.5" r="4"/><circle cx="20" cy="43.5" r="4"/>
      <circle cx="20" cy="65.5" r="4"/>
    </g>
    <path class="fig-warn fig-blink" d="M100 36l10 10M110 36l-10 10"/>
    <path class="fig-muted" d="M96 58h28"/>`,

  /* The same inputs twice, and the same answer twice. */
  reproducible: `
    <g class="fig-stroke">
      <rect x="8" y="18" width="40" height="24" rx="4"/>
      <rect x="8" y="52" width="40" height="24" rx="4"/>
    </g>
    <g class="fig-mono fig-muted">
      <path d="M16 30h24M16 64h24"/>
    </g>
    <path class="fig-accent" d="M48 30h20M48 64h20"/>
    <g class="fig-accent">
      <path d="M74 26h16M74 34h16"/>
      <path d="M74 60h16M74 68h16"/>
    </g>
    <path class="fig-accent fig-eq fig-blink" d="M100 40h18M100 50h18"/>`,

  /* One option chosen, and the ones that lost still on the page. */
  arguable: `
    <g class="fig-stroke">
      ${[0, 1, 2, 3].map((i) => `<rect x="20" y="${12 + i * 18}" width="92" height="13" rx="3"/>`).join('')}
    </g>
    <path class="fig-accent-fill fig-chosen" d="M20 12h92v13H20z"/>
    <path class="fig-accent fig-draw" d="M26 18.5l3.5 3.5 6 -7"/>
    <g class="fig-muted">
      <path d="M28 36.5h60M28 54.5h48M28 72.5h66"/>
    </g>`,
};

export function builtFigure(kind) {
  return `<svg class="stage-fig" viewBox="0 0 132 92" role="img" aria-hidden="true"
       focusable="false">${BUILT[kind] || ''}</svg>`;
}

/* ---- the stack strip ----------------------------------------------------- */

/* These are not the vendors' logos.
 *
 * Two reasons, and the second is the one that decided it. A brand mark is a
 * trademark, and redrawing eleven of them from memory produces eleven slightly
 * wrong trademarks. And every one of them would have to be fetched or embedded
 * as raster - this page loads nothing from anywhere, which is the first thing it
 * claims about itself, and a strip of logos would be an odd place to break that.
 *
 * So each technology gets a mark drawn in the same line language as the rest of
 * the page, saying what the thing DOES, next to its name in text. The name is
 * what a reader actually reads in a strip like this one. */
const STACK = [
  ['Python', `
    <path class="sm-line" d="M12 3.5h4.5a3 3 0 0 1 3 3V10a3 3 0 0 1-3 3H8.5a3 3 0 0 0-3 3v1.5"/>
    <path class="sm-line" d="M12 20.5H7.5a3 3 0 0 1-3-3V14a3 3 0 0 1 3-3h8a3 3 0 0 0 3-3V6.5"/>
    <circle class="sm-dot" cx="8.4" cy="7.2" r="1.15"/>
    <circle class="sm-dot" cx="15.6" cy="16.8" r="1.15"/>`],
  ['FastAPI', `
    <rect class="sm-line" x="3.5" y="3.5" width="17" height="17" rx="4.5"/>
    <path class="sm-fill" d="M13.4 6.2l-5.1 7h3.3l-1 4.6 5.1-7h-3.3z"/>`],
  ['PostgreSQL', `
    <ellipse class="sm-line" cx="12" cy="6.4" rx="7" ry="2.9"/>
    <path class="sm-line" d="M5 6.4v11.2c0 1.6 3.1 2.9 7 2.9s7-1.3 7-2.9V6.4"/>
    <path class="sm-muted" d="M5 12c0 1.6 3.1 2.9 7 2.9s7-1.3 7-2.9"/>`],
  ['TimescaleDB', `
    <ellipse class="sm-line" cx="12" cy="6.4" rx="7" ry="2.9"/>
    <path class="sm-line" d="M5 6.4v11.2c0 1.6 3.1 2.9 7 2.9s7-1.3 7-2.9V6.4"/>
    <path class="sm-accent" d="M8.2 15.4l2.4-2.9 2.1 1.8 3.1-4"/>`],
  ['OR-Tools CP-SAT', `
    <rect class="sm-line" x="3.5" y="3.5" width="17" height="17" rx="2.5"/>
    <path class="sm-muted" d="M9.2 3.5v17M14.8 3.5v17M3.5 9.2h17M3.5 14.8h17"/>
    <rect class="sm-fill" x="9.2" y="9.2" width="5.6" height="5.6"/>`],
  ['scikit-learn', `
    <path class="sm-muted" d="M4 20h16M4 20V4"/>
    <path class="sm-accent" d="M5.6 17.6L18.4 6.6"/>
    <circle class="sm-dot" cx="8" cy="16.4" r="1.1"/>
    <circle class="sm-dot" cx="11.6" cy="12.2" r="1.1"/>
    <circle class="sm-dot" cx="15.4" cy="10.6" r="1.1"/>`],
  ['MQTT', `
    <circle class="sm-fill" cx="6.4" cy="17.6" r="1.9"/>
    <path class="sm-line" d="M6.4 12.2a5.4 5.4 0 0 1 5.4 5.4"/>
    <path class="sm-line" d="M6.4 7.2a10.4 10.4 0 0 1 10.4 10.4"/>
    <path class="sm-accent" d="M6.4 3.2a14.4 14.4 0 0 1 14.4 14.4"/>`],
  ['Docker', `
    <path class="sm-line" d="M3.5 12.6h14.2a3.6 3.6 0 0 1-3.6 6H8.4a4.9 4.9 0 0 1-4.9-4.9z"/>
    <path class="sm-muted" d="M6.4 12.6V9.4h2.9v3.2M10.6 12.6V9.4h2.9v3.2M10.6 8.2V5h2.9v3.2"/>
    <path class="sm-accent" d="M17.7 11.2c1.4-.9 2.6-.6 3.3 0"/>`],
  ['nginx', `
    <path class="sm-line" d="M12 3.2l7.6 4.4v8.8L12 20.8 4.4 16.4V7.6z"/>
    <path class="sm-accent" d="M9.2 15.8V8.4l5.6 7.2V8.4"/>`],
  ['ES modules', `
    <path class="sm-line" d="M9 5.6L4.2 12 9 18.4M15 5.6L19.8 12 15 18.4"/>
    <path class="sm-accent" d="M13.4 5.2l-2.8 13.6"/>`],
];

/* One chip. `aria-hidden` on the drawing because the name beside it already
   says what this is, and a screen reader does not need it twice. */
function stackChip([name, art]) {
  return `<li class="stack-chip">
      <svg class="stack-mark" viewBox="0 0 24 24" role="img" aria-hidden="true"
           focusable="false">${art}</svg>
      <span>${name}</span>
    </li>`;
}

/* The strip is the same list three times.
 *
 * Every copy slides left by exactly its own width and the animation restarts, so
 * what the reader sees at the end of a cycle is the next copy sitting where the
 * last one began - identical content in the same place, which is what makes the
 * loop invisible. Four copies rather than two: at the end of a cycle only the
 * copies behind the first one are still on screen, so what they cover has to be
 * at least the width of the window. One copy measures about 1200px, and three of
 * them behind the first carry it past any monitor this will be shown on. Two
 * copies would leave a gap crossing the strip once per cycle on anything wider
 * than about 1200px, which is most of them.
 *
 * The extra copies are decoration and are hidden from assistive technology,
 * which would otherwise read the whole stack out three times. */
const STACK_COPIES = 4;

export function stackStrip() {
  const items = STACK.map(stackChip).join('');
  let out = `<ul class="stack-track">${items}</ul>`;
  for (let i = 1; i < STACK_COPIES; i += 1) {
    out += `<ul class="stack-track" aria-hidden="true">${items}</ul>`;
  }
  return out;
}

export function stageFigure(kind) {
  return `<svg class="stage-fig" viewBox="0 0 132 92" role="img" aria-hidden="true"
       focusable="false">${FIGURES[kind] || ''}</svg>`;
}
