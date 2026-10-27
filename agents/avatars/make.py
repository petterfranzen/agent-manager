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

# Saito: patient sniper. One cybernetic eye with a reticle.
skin = "#d6ab84"
saito = frame("#ff4d6d", person(
    skin, "#27242c",
    HEAD.format(skin=skin),
    '<path d="M172 214C164 118 222 100 262 102C312 104 352 132 340 212C330 170 300 150 254 152C232 150 208 140 196 130C186 156 180 182 172 214Z" fill="#1d1a1c"/>',
    """
<path d="M198 198L238 204M274 204L316 196" stroke="#1d1a1c" stroke-width="9" stroke-linecap="round"/>
<path d="M202 230Q220 216 238 230Q220 240 202 230Z" fill="#f4efe6"/>
<circle cx="220" cy="229" r="7" fill="#2b1d16"/>
<circle cx="292" cy="230" r="30" fill="#1b1d22" stroke="#8d939c" stroke-width="5"/>
<circle cx="292" cy="230" r="17" fill="#ff4d6d"/>
<circle cx="292" cy="230" r="17" fill="url(#glow)"/>
<path d="M292 206V218M292 242V254M268 230H280M304 230H316" stroke="#ffd0d8" stroke-width="3"/>
<circle cx="292" cy="230" r="4" fill="#fff"/>
<path d="M256 238L248 276H264" fill="none" stroke="#a87e5c" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M232 308H282" stroke="#7a4a3a" stroke-width="5" stroke-linecap="round"/>
""",
    collar='<path d="M210 380L256 420L302 380L320 392L256 470L192 392Z" fill="#3a3540"/>'),
    extra_bg='<g stroke="#ff4d6d" stroke-opacity=".25" stroke-width="2" fill="none"><circle cx="256" cy="236" r="150"/><path d="M256 20V100M256 372V452M40 236H120M392 236H472"/></g>')

# Togusa: the most human of the team. Detective, careful, coat and tie.
skin = "#e0b391"
togusa = frame("#b58cff", person(
    skin, "#b89b6e",
    HEAD.format(skin=skin),
    '<path d="M170 240C158 160 190 100 258 98C326 98 356 160 342 240C338 214 334 194 330 176C300 170 262 150 244 132C230 160 200 176 182 182C178 204 174 222 170 240Z" fill="#5a3a24"/>'
    '<path d="M172 252C170 274 174 290 182 300L186 262ZM340 252C342 274 338 290 330 300L326 262Z" fill="#5a3a24"/>',
    """
<path d="M200 204Q220 196 240 204M272 204Q292 196 312 204" fill="none" stroke="#5a3a24" stroke-width="7" stroke-linecap="round"/>
<path d="M204 230Q222 216 240 230Q222 240 204 230Z" fill="#fff"/><path d="M272 230Q290 216 308 230Q290 240 272 230Z" fill="#fff"/>
<circle cx="222" cy="229" r="7" fill="#3b2a1e"/><circle cx="290" cy="229" r="7" fill="#3b2a1e"/>
<circle cx="224" cy="227" r="2" fill="#fff"/><circle cx="292" cy="227" r="2" fill="#fff"/>
<path d="M256 240L248 276H264" fill="none" stroke="#b5876a" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M232 304Q256 318 282 302" fill="none" stroke="#8a4f3c" stroke-width="5" stroke-linecap="round"/>
""",
    collar='<path d="M216 380L256 512L296 380Z" fill="#f2f0ea"/><path d="M250 392H262L268 470L256 500L244 470Z" fill="#3b2f5c"/>'
           '<path d="M188 384L232 380L256 512L170 420Z" fill="#cdb183"/><path d="M324 384L280 380L256 512L342 420Z" fill="#cdb183"/>'))

# Tachikoma: curious, cheerful little think-tank. A playful round robot.
tachikoma = frame("#3ad1ff", """
<defs><radialGradient id="shell" cx="40%" cy="35%" r="70%"><stop offset="0" stop-color="#7fe0ff"/><stop offset=".55" stop-color="#2b8fd8"/><stop offset="1" stop-color="#16467a"/></radialGradient></defs>
<g stroke="#9fb4c8" stroke-width="12" stroke-linecap="round" fill="none">
  <path d="M190 330L132 380L112 460"/><path d="M322 330L380 380L400 460"/>
  <path d="M220 350L200 420L210 480"/><path d="M292 350L312 420L302 480"/>
</g>
<g fill="#cfe2f2"><circle cx="112" cy="462" r="12"/><circle cx="400" cy="462" r="12"/><circle cx="210" cy="482" r="12"/><circle cx="302" cy="482" r="12"/></g>
<ellipse cx="256" cy="360" rx="110" ry="40" fill="#000" opacity=".25"/>
<circle cx="256" cy="250" r="120" fill="url(#shell)" stroke="#0e2d50" stroke-width="5"/>
<path d="M170 200Q256 150 342 200" fill="none" stroke="#ffffff" stroke-opacity=".35" stroke-width="8" stroke-linecap="round"/>
<g>
  <circle cx="210" cy="262" r="34" fill="#f5fbff" stroke="#0e2d50" stroke-width="5"/>
  <circle cx="302" cy="262" r="34" fill="#f5fbff" stroke="#0e2d50" stroke-width="5"/>
  <circle cx="216" cy="266" r="16" fill="#0b1830"/><circle cx="308" cy="266" r="16" fill="#0b1830"/>
  <circle cx="222" cy="259" r="6" fill="#fff"/><circle cx="314" cy="259" r="6" fill="#fff"/>
  <circle cx="256" cy="312" r="9" fill="#0b1830"/>
</g>
<path d="M256 130V82" stroke="#9fb4c8" stroke-width="6" stroke-linecap="round"/>
<circle cx="256" cy="74" r="12" fill="#3ad1ff"/><circle cx="256" cy="74" r="26" fill="url(#glow)"/>
<g fill="#3ad1ff" opacity=".9"><path d="M372 120l6 14 14 6-14 6-6 14-6-14-14-6 14-6z"/><path d="M134 150l4 9 9 4-9 4-4 9-4-9-9-4 9-4z"/></g>
""")

for name, svg in {"tachikoma": tachikoma, "batou": batou, "ishikawa": ishikawa,
                  "saito": saito, "togusa": togusa}.items():
    (HERE / f"{name}.svg").write_text(svg)
    print("wrote", name)
