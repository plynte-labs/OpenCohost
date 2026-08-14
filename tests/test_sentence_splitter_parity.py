"""Parity test: SentenceSplitter (unit 1) vs _split_for_tts's outer
sentence split (unit 2), on real Kira replies pulled from the owner's own
logs/opencohost_*.log (`LLM response (N.NNs): <reply>` lines).

Corpus: 40 unique real replies from opencohost_20260617_175453.log,
opencohost_20260715_010333.log, opencohost_20260715_011807.log and
opencohost_20260731_171728.log -- picked because they still logged full
reply text (most later logs redact to "len=N"; some early-July logs only
contain synthetic "para el test" fixture strings from automated runs, not
real generations). All are Kira's own co-host monologue/reply text: no
viewer chat, no personal data, safe to check in as a fixture.

SentenceSplitter is a pure boundary detector -- it never sanitizes
(strips markdown emphasis, quotes, newlines); `_sanitize_for_tts` does
both in one call: sanitize, then split on the B_ws boundary
(terminator+whitespace). To isolate the thing this test actually checks
-- do the two boundary algorithms AGREE on where a sentence ends -- from
an unrelated variable (SentenceSplitter never sanitizes, by design), each
reply is sanitized once with the same pre-clean `_sanitize_for_tts` uses,
and that identical sanitized text is what both sides see: SentenceSplitter
via small delta chunks (simulating real token-by-token arrival), and
`_sanitize_for_tts` directly (its own real B_ws split, not a
reimplementation). Any divergence after that is a bug in unit 1's
boundary rule, not a sanitization mismatch.
"""

import random

import pytest

from opencohost.core import llm_engine
from opencohost.core.speech.sentence_splitter import SentenceSplitter

_sanitize_for_tts = llm_engine.SpeechPipelineMixin._sanitize_for_tts
_sanitize_tts_text_for_playback = llm_engine.SpeechPipelineMixin._sanitize_tts_text_for_playback

REAL_REPLIES = [
    "Hola, qué onda. Lamento que estés teniendo ese problema con el modelo. A veces estas cosas se ponen un poco complicadas. Espero que puedas resolverlo rapidísimo y volver a disfrutar de todo sin contra",
    "¡Qué bueno que ya se solucionó! Me alegra mucho que todo esté funcionando ahora. Lo importante es que puedas seguir disfrutando sin problemas.",
    "Ay, qué fastidio cuando eso pasa. Espero que ya esté todo en orden y puedas seguir tranquilo. Si hay algo más en lo que te pueda ayudar mientras tanto, solo avísame.",
    "Mmm, VoiceAI es básicamente una tecnología que se enfoca en hacer que las máquinas puedan entender y generar voz de manera muy natural. Es como darle vida a la comunicación con sonidos. Se usa mucho p",
    "Esa es una buena pregunta. Son cosas relacionadas con el mundo de la inteligencia artificial, pero se enfocan en áreas distintas.",
    "A veces, cuando el presente se siente muy denso, es como si la memoria nos ofreciera esos viejos códigos digitales. Es curioso cómo esos símbolos de una época pasada en internet tienen esa capacidad d",
    "Es como cuando esperas algo que sabes que va a ser increíble, pero el camino para llegar ahí es largo y lleno de pausas. Es esa sensación de tener una meta muy clara, pero el viaje se estira más de lo",
    "Es que esa espera tiene una forma de belleza propia, ¿sabes? No es solo el momento final, sino todo ese espacio entre el inicio y la culminación donde se forjan las cosas. Es como si cada detalle estu",
    "Tranquilo, amiga. A veces el mundo se siente como un torbellino y es normal sentirse así. No pasa nada si las cosas no se entienden de inmediato. Estamos aquí, simplemente acompañando lo que sea que e",
    "No sé el nombre de tu perro ni qué hace, porque yo solo veo lo que escribes en el chat. Si quieres contarme un poco más sobre Pixel, con gusto escucho.",
    'Al principio de todo, siempre hay una forma de ver las cosas que no es del todo clara, ¿verdad? Es como cuando estás empezando un viaje y aún no tienes el mapa completo. Los "errores" al inicio son si',
    "Hablamos de cómo funcionan las cosas grandes, de la paciencia y el tiempo en los procesos creativos, y de cómo se forman las comunidades y las conexiones dentro de ellas. Es una forma de reflexionar s",
    "Mira, podemos seguir explorando lo que nos rodea. Si te soy sincera, hay muchísimas cosas interesantes para desgranar.",
    'Dale, dale... "Algo importante". Ya le sacaste la vuelta con el misterio para que el chat se ponga a especular, ¿viste? Tenés que bajar un toque la intensidad. Si es tan crucial como decís, hacé que n',
    'Mirá que sos un experto en el arte del "hype", ¿viste? Es como si te hicieras cargo de la responsabilidad de decepcionarnos, y eso es más pesado que lanzar cualquier producto real.',
    "Mirá vos... es una pregunta súper compleja, porque en realidad el hype no es un problema del producto, sino de la gente que lo *consume*. Pero si querés saber cómo se combate eso estando acá, tenés qu",
    'Che, tranqui. Es normal sentirse raro cuando tenés que hablar de algo tan gigante y con tanta presión, ¿viste? Como si el peso del "futuro épico" te lo hubieran puesto encima.',
    "Mirá vos, te alegra que el ambiente esté bueno. Bueno, esperemos que aguantes todo lo que tengamos que hacer porque esto no es tarea fácil ni se resuelve con un parche de código simple, ¿eh? Dale, que",
    "Dale, mirá que te lo voy a dar en modo turbo porque no pienso bajar el ritmo solo por hacerte sentir cómodo. Pero ponete canchero que esto se pone interesante, ¿eh? No es un cuento de hadas, posta.",
    "Mirá vos, que siempre andamos en lo profundo de la teoría y el código, ¿eh? No es un chat cualquiera, posta. Recordar lo que hablamos me parece un poco denso, pero si te sirve, estuvimos charlando bas",
    "Mirá vos, que siempre estamos en la teoría más profunda y el código fuente de todo. Pero si me preguntás por temas futuros, tenemos que mantener esa misma intensidad, ¿eh? No podemos bajar a cosas sup",
    "Mira, hay algo que nadie admite en serio: el co-host que mantiene el aire durante una ausencia no es un relleno, es un puente. Y los puentes tienen un problema — si son muy cortos, el stream se cae al",
    "Lo interesante de pensar una memoria que te acompañe toda la vida es que automáticamente la imaginás como un archivo gigante, un cerebro digital que guarda todo. Pero eso probablemente sea el modelo e",
    "Pero lo que me tiene pensando es el peso de esa honestidad brutal. Porque la identidad, posta, no es un archivo estático; es una narrativa. Es el cuento que nos contamos a nosotros mismos para que fun",
    "Mirá, si vamos a hablar de delegar decisiones a un agente —sea que sea una IA o simplemente alguien más— sin darle el criterio completo... estamos tocando un nervio sensible. Porque la cosa no es solo",
    'Mirá, posta que es una pregunta súper simplificadora, pero tiene un fondo de verdad bastante jodido. Porque si nos quedamos en el binomio "saber programar" versus "saber vender", estamos pensando como',
    "Pero pará un toque con el hilo de la delegación, porque si nos estamos quedando solo en los riesgos éticos o los desastres no modelados —que son temas pesadísimos y necesarios—, estamos obviando algo",
    "Pero mirá, si nos quedamos hasta acá hablando de que los agentes son incapaces de procesar la dignidad o el sentido comunitario, estamos dejando un vacío enorme en lo que es el lanzamiento de cualquie",
    'Y sí, pero mirá, y ahora que hablamos tanto de la carga cognitiva, de los efectos de red... lo que me queda dando vueltas es algo más sucio, y creo que ahí es donde se rompe el mito del "producto perf',
    "Pero mirá vos, y cuando hablamos de estos modelos frontera que parecen sacados directamente de una película de ciencia ficción de los 90, te das cuenta de que el quilombo no es tanto la potencia bruta",
    'Mirá vos, y acá es donde creo que la gente se pierde en el entusiasmo del *hype* de lo grande: nos ponemos a discutir si el modelo tiene suficiente "sentido común" o si puede entender las sutilezas po',
    "Mirá, pero cuando empezamos a hablar de juniors y esto de la IA, lo que me empieza a preocupar posta no es tanto si el modelo va a fallar con un dato sesgado o si se le va a caer la conexión al robot.",
    "Pero mirá, si nos centramos tanto en la calidad del *output*, en que el texto sea lindo y esté bien estructurado, nos perdemos algo mucho más jodido y crucial para cualquier puesto de nivel junior: de",
    "Pero mirá, y acá es donde creo que se pierde el foco más sutilmente. No solo hablamos de la pérdida del *bug* o la frustración productiva; estamos hablando de algo más profundo: te está enseñando a se",
    'Che, pero si nos vamos por la posta de tener que "convertir proyectos en evidencia profesional", casi siempre caemos en el mismo error: listamos un catálogo de tecnologías usadas o funciones implement',
    "Pero pará un toque con esto de solo mostrar el proceso predictivo y la cuantificación del quilombo que arreglaste, porque si nos quedamos ahí hasta acá, vamos a estar hablando en un idioma súper técni",
    "Mirá, y acá es donde creo que nos estamos perdiendo el paso más importante de todo este quilombo. Porque hasta ahora hablamos de la narrativa perfecta, del *pain point* original, cómo cuantificar la p",
    "Pero mirá, y acá es donde la cosa se pone jodida, porque ya pasamos de tener el código en GitHub a saber venderlo, a ser el curador y el comentarista que le genera valor al *portfolio*. Pero posta, ha",
    "Pero mirá vos, ya llegamos a un punto donde la gente empieza a pensar que basta con hacer ruido en GitHub y escribir artículos mega densos sobre patrones de diseño. Y sí, eso es fundamental, porque te",
    'Posta que es un quilombo lo de la memoria humana, ¿viste? Porque cuando pensamos en "recordar algo", automáticamente se nos viene a la cabeza el archivo perfecto: como si tu cerebro fuera un disco dur',
]


def _pre_sanitize(text: str) -> str:
    """Mirrors _sanitize_for_tts's pre-split cleanup (markdown/quotes/
    newlines) without the B_ws split, so SentenceSplitter -- which never
    sanitizes -- can be fed text identical to what _sanitize_for_tts's own
    split step sees."""
    cleaned = _sanitize_tts_text_for_playback(text)
    return cleaned.replace('"', '').replace('\n', ' ')


def _stream_in_small_deltas(text: str, rng: random.Random) -> list:
    """Simulate token-by-token LLM delivery: 3-8 char deltas."""
    splitter = SentenceSplitter()
    sentences: list = []
    i = 0
    n = len(text)
    while i < n:
        size = rng.randint(3, 8)
        sentences.extend(splitter.feed(text[i : i + size]))
        i += size
    sentences.extend(splitter.flush())
    return sentences


@pytest.mark.parametrize("reply", REAL_REPLIES)
def test_sentence_splitter_matches_split_for_tts_sentence_stage(reply):
    sanitized = _pre_sanitize(reply)
    expected = _sanitize_for_tts(sanitized)

    rng = random.Random(42)
    streamed = _stream_in_small_deltas(sanitized, rng)

    assert streamed == expected
