"""Writes the agents' avatar SVGs. Original designs, only loosely nodding at the
Ghost in the Shell characters they're named after. Run render.js to make PNGs."""
import pathlib

HERE = pathlib.Path(__file__).parent


def frame(accent, body, extra_bg=""):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
<defs>
  <radialGradient id="bg" cx="50%" cy="40%" r="70%">
    <stop offset="0" stop-color="#16203a"/><stop offset="1" stop-color="#05070d"/>
  </radialGradient>
  <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse">
    <path d="M32 0H0V32" fill="none" stroke="{accent}" stroke-opacity=".08" stroke-width="1"/>
  </pattern>
  <radialGradient id="glow" cx="50%" cy="50%" r="50%">
    <stop offset="0" stop-color="{accent}" stop-opacity=".9"/><stop offset="1" stop-color="{accent}" stop-opacity="0"/>
  </radialGradient>
</defs>
<rect width="512" height="512" fill="url(#bg)"/>
<rect width="512" height="512" fill="url(#grid)"/>
<circle cx="256" cy="236" r="200" fill="none" stroke="{accent}" stroke-opacity=".35" stroke-width="3"/>
{extra_bg}
{body}
</svg>
"""


def person(skin, jacket, head, hair, face, collar="", extra=""):
    return f"""
<path d="M80 512C92 410 168 372 256 370C344 372 420 410 432 512Z" fill="{jacket}"/>
{collar}
<rect x="218" y="296" width="76" height="92" rx="22" fill="{skin}" filter="brightness(.9)"/>
<path d="M218 330Q256 360 294 330V388H218Z" fill="#000" opacity=".18"/>
<ellipse cx="{170 if 'wide' not in head else 162}" cy="238" rx="13" ry="24" fill="{skin}"/>
<ellipse cx="{342 if 'wide' not in head else 350}" cy="238" rx="13" ry="24" fill="{skin}"/>
{head.replace('wide', '')}
{hair}
{face}
{extra}
"""

HEAD = '<path d="M176 196C176 112 336 112 336 196V252C336 312 300 346 256 346C212 346 176 312 176 252Z" fill="{skin}"/>'
HEAD_WIDE = 'wide<path d="M166 200C166 108 346 108 346 200V258C346 300 330 334 300 346L256 354L212 346C182 334 166 300 166 258Z" fill="{skin}"/>'

# Batou: big, blunt, dependable. Pale cropped hair, cylindrical lens eyes.
skin = "#c99a74"
batou = frame("#ffb347", person(
    skin, "#2c3328",
    HEAD_WIDE.format(skin=skin),
    '<path d="M164 196L172 128L192 146L204 112L224 136L240 102L258 130L276 102L290 134L310 110L322 144L342 126L348 196C330 160 300 150 256 150C212 150 182 160 164 196Z" fill="#e9e4cf"/>',
    """
<path d="M198 196H242M270 196H314" stroke="#7a5a40" stroke-width="10" stroke-linecap="round"/>
<rect x="194" y="214" width="56" height="34" rx="8" fill="#3a3f47"/>
<rect x="262" y="214" width="56" height="34" rx="8" fill="#3a3f47"/>
<circle cx="222" cy="231" r="17" fill="#15181d" stroke="#9aa1ab" stroke-width="4"/>
<circle cx="290" cy="231" r="17" fill="#15181d" stroke="#9aa1ab" stroke-width="4"/>
<circle cx="222" cy="231" r="7" fill="#ffb347"/><circle cx="290" cy="231" r="7" fill="#ffb347"/>
<circle cx="219" cy="227" r="2.5" fill="#fff"/><circle cx="287" cy="227" r="2.5" fill="#fff"/>
<path d="M256 240L246 282H264" fill="none" stroke="#9c7356" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M226 310Q256 318 290 304" fill="none" stroke="#6e4a35" stroke-width="6" stroke-linecap="round"/>
<path d="M200 300Q256 360 312 300" fill="none" stroke="#a37c5c" stroke-width="3" stroke-dasharray="2 6" opacity=".7"/>
""",
    collar='<path d="M196 384L256 440L316 384L340 400L256 512L172 400Z" fill="#3b4434"/><path d="M226 392L256 430L286 392" fill="#1d2119"/>'))

# Ishikawa: veteran intel man. Dark curly hair and beard, shades full of data.
skin = "#d2a682"
ishikawa = frame("#39ff88", person(
    skin, "#2a3a2e",
    HEAD.format(skin=skin),
    '<path d="M170 214C160 120 214 92 256 94C302 92 354 122 342 214C334 176 320 158 300 150C280 166 232 166 212 150C192 160 178 180 170 214Z" fill="#3a2a20"/>'
    '<g fill="#3a2a20"><circle cx="196" cy="132" r="18"/><circle cx="226" cy="110" r="20"/><circle cx="262" cy="102" r="20"/><circle cx="298" cy="112" r="20"/><circle cx="324" cy="136" r="18"/></g>',
    """
<path d="M176 250C178 318 214 352 256 354C298 352 334 318 336 250C326 280 304 292 286 290C274 274 238 274 226 290C208 292 186 280 176 250Z" fill="#3a2a20"/>
<path d="M234 300Q256 292 278 300" fill="none" stroke="#8a4f3c" stroke-width="5" stroke-linecap="round"/>
<path d="M256 238L248 272H262" fill="none" stroke="#a37c5c" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
<rect x="186" y="204" width="62" height="40" rx="12" fill="#07140c" stroke="#1b1b1b" stroke-width="5"/>
<rect x="264" y="204" width="62" height="40" rx="12" fill="#07140c" stroke="#1b1b1b" stroke-width="5"/>
<path d="M248 220H264" stroke="#1b1b1b" stroke-width="5"/>
<g stroke="#39ff88" stroke-width="3" stroke-linecap="round" opacity=".85">
  <path d="M196 216H218M224 216H236M196 226H210M216 226H238M196 236H228"/>
  <path d="M274 216H290M296 216H316M274 226H300M274 236H286M292 236H314"/>
</g>
<rect x="334" y="222" width="16" height="22" rx="4" fill="#5a6470"/>
<path d="M350 236C380 250 390 300 372 360S330 450 360 512" fill="none" stroke="#39ff88" stroke-width="4" opacity=".7"/>
""",
    collar='<path d="M200 382L256 470L312 382" fill="#1f2b22"/>'))

for name, svg in {"major": major, "tachikoma": tachikoma, "batou": batou, "ishikawa": ishikawa,
                  "togusa": togusa}.items():
    (HERE / f"{name}.svg").write_text(svg)
    print("wrote", name)
