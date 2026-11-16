"""Rule gate: which self-knowledge topics does an utterance ask about.

Same approach as the vision rule gate (``core/vision/gate.py``): normalized
es/en regex, no model call, no state. The gate never logs or keeps the text.
"""
from __future__ import annotations

from opencohost.core.self_knowledge.facts import (
    ALL_TOPICS,
    TOPIC_CAPABILITIES,
    TOPIC_COMMANDS,
    TOPIC_CREATOR,
    TOPIC_GPU,
    TOPIC_IDENTITY,
    TOPIC_INTERLOCUTOR,
    TOPIC_MODEL,
    TOPIC_RUNTIME,
    TOPIC_TIME,
)
from opencohost.core.vision.gate import DATA_QUESTION_PATTERNS, _normalize, _rx

_RULES = {
    TOPIC_TIME: _rx(
        r"\bque hora(?:s)? (?:es|son)\b", r"\bque hora tienes\b",
        r"\b(?:dime|dame|dices|puedes decir(?:me)?) la hora\b",
        r"\bhora (?:actual|exacta)\b",
        r"\bque (?:dia|fecha) es\b", r"\bfecha de hoy\b", r"\ben que fecha\b",
        r"\bwhat time\b", r"\bcurrent time\b", r"\btell me the time\b",
        r"\bwhat (?:day|date) is\b", r"\btodays date\b", r"\bwhat is the date\b",
    ),
    TOPIC_MODEL: _rx(
        r"\bque modelo\b", r"\bcon que modelo\b", r"\bcual es tu modelo\b",
        r"\bque llm\b",
        # "tu/su modelo" is also how the owner names the avatar (VRM/3D) model
        # and "el modelo" alone is too common, so those need a qualifier.
        r"\b(?:tu|su) modelo\b(?! (?:vrm|3d|de (?:negocio|avatar|ropa)))",
        r"\b(?:inventaste|inventas|inventaron) (?:el|tu|su) modelo\b",
        r"\bmodelo (?:base|de lenguaje)\b",
        r"\bque (?:ia|motor|cerebro) (?:eres|usas|corres|tienes|te (?:mueve|corre|hace funcionar))\b",
        r"\bcon que (?:ia|motor|cerebro)\b",
        r"\bwhich model\b", r"\bwhat model\b", r"\bwhat llm\b", r"\bwhich llm\b",
        r"\bbase model\b", r"\blanguage model\b", r"\byour model\b",
        r"\b(?:made up|invented|making up) (?:the|your) model\b",
        r"\bwhat (?:ai|engine|brain) (?:are you|do you (?:use|run))\b",
        r"\bcomo (?:te )?(?:instalo|instalar|instalamos)\b(?! (?:un|el|este|ese|mod|juego))",
        r"\bcomo instalo opencohost\b", r"\bhow (?:do|can|should) i install (?:you|opencohost)\b",
        r"\bcomo (?:te )?(?:cambio|cambiamos|puedo cambiar) (?:de |el |tu |su )?modelo\b(?! (?:vrm|3d|de (?:negocio|avatar|ropa)|del? avatar))",
        r"\bcambiar (?:de|el|tu|su) modelo\b(?! (?:vrm|3d|de (?:negocio|avatar|ropa)|del? avatar))",
        r"\bhow (?:do|can|should) i (?:change|switch|swap)(?: the| your)? model\b(?! (?:vrm|3d|de (?:negocio|avatar|ropa)|del? avatar))",
        r"\b(?:change|switch|swap) (?:the|your) model\b(?! (?:vrm|3d|de (?:negocio|avatar|ropa)|del? avatar))",
        r"\beres (?:un|solo un|simplemente un) modelo\b(?! de (?:negocio|avatar|ropa))",
        r"\bare you (?:just )?a (?:language )?model\b",
        r"\b(?:eres|are you) (?:gemma|qwen|llama|gpt|chatgpt|mistral|phi|deepseek|claude|gemini)\b",
    ),
    TOPIC_RUNTIME: _rx(
        r"\blocal (?:o|u|or) (?:en la )?(?:nube|cloud)\b",
        r"\b(?:nube|cloud) (?:o|or) (?:en )?local\b",
        r"\beres (?:local|cloud|de la nube)\b", r"\ben la nube\b",
        r"\bdonde (?:corres|estas corriendo|te ejecutas|estas ejecutandote|funcionas|estas funcionando|vives)\b",
        r"\bdesde donde (?:interactuas|hablas|corres|funcionas|me hablas)\b",
        r"\bare you (?:local|cloud|running locally|in the cloud)\b", r"\bin the cloud\b",
        r"\bwhere (?:do you run|are you (?:running|hosted|located))\b",
        r"\bwhere do you (?:live|interact)\b", r"\bfrom where do you\b",
    ),
    TOPIC_GPU: _rx(
        r"\bvram\b", r"\bgpu\b", r"\btarjeta (?:grafica|de video)\b",
        r"\bgraphics card\b", r"\bvideo memory\b",
    ),
    TOPIC_IDENTITY: _rx(
        r"\bquien eres\b", r"\bque eres\b", r"\bcomo te llamas\b", r"\btu nombre\b",
        r"\bque es opencohost\b", r"\bde que trata (?:esta app|opencohost)\b",
        r"\bwho are you\b", r"\bwhat are you\b", r"\byour name\b",
        r"\bwhat is opencohost\b",
        r"\bopen ?cohost\b",
        r"\beres (?:un|solo un|simplemente un) modelo\b(?! de (?:negocio|avatar|ropa))",
        r"\bare you (?:just )?a (?:language )?model\b",
        r"\beres (?:una )?(?:novia|pareja|companion)\b", r"\bnovia virtual\b(?=.*\b(?:eres|tu|kira)\b)",
        r"\bare you (?:a |an )?(?:(?:ai|virtual) )?(?:girlfriend|companion)\b",
    ),
    TOPIC_CAPABILITIES: _rx(
        r"\bque (?:puedes|sabes) hacer\b", r"\bque haces\b", r"\bpara que sirves\b",
        r"\bcapacidades\b", *DATA_QUESTION_PATTERNS,
        r"\bwhat can you do\b", r"\bwhat do you do\b", r"\bcapabilities\b",
    ),
    TOPIC_COMMANDS: _rx(r"\bcomandos?\b", r"\bcommands?\b"),
    TOPIC_CREATOR: _rx(
        r"\bquien te (?:creo|hizo|programo|desarrollo|construyo|entreno|diseno|invento)\b",
        r"\b(?:fuiste|eres|has sido) (?:creada|creado|desarrollada|desarrollado|programada|programado|construida|construido|hecha|hecho|entrenada|entrenado|disenada|disenado)\b",
        r"\bque (?:empresa|compania|equipo|laboratorio) (?:te|esta detras|es tu|fue)\b",
        r"\bquien (?:creo|hizo|desarrollo|programo|construyo|diseno) (?:(?:este|esta|el|la) )?(?:proyecto|app|aplicacion|plataforma|programa|opencohost|open cohost|kira)\b",
        r"\bquien es (?:el |la )?(?:creador|creadora|desarrollador|desarrolladora|programador|programadora) de (?:opencohost|open cohost|kira|este proyecto|esta app)\b",
        r"\bquien es tu (?:creador|desarrollador|programador)\b",
        # Bare "repositorio/github/codigo fuente" fit any topic; they count only
        # when aimed at Kira or OpenCohost.
        r"\b(?:tu|su) (?:repositorio|repo|codigo(?: fuente)?)\b",
        r"\b(?:repositorio|repo|codigo fuente|github) (?:de|del) (?:opencohost|open cohost|kira)\b",
        r"\bdonde (?:esta|queda) (?:el )?(?:repositorio|repo|codigo fuente)\b",
        r"\bwho (?:made|created|built|developed|programmed|trained|designed) you\b",
        r"\bwere you (?:created|made|built|developed|programmed|trained|designed)\b",
        r"\bwhich (?:company|team|lab) (?:made|created|built|developed|trained) you\b",
        r"\bwho (?:created|made|built|developed) (?:this|the) (?:project|app|platform)\b",
        r"\bwho is behind (?:you|this)\b",
        r"\byour (?:creator|developer|maker)\b",
        r"\bwhere(?: is|'s| s) (?:the|your) (?:repository|repo|source code)\b",
        r"\byour (?:repository|repo|source code)\b",
        r"\b(?:repository|repo|source code|github) (?:of|for) (?:opencohost|open cohost|kira)\b",
        r"\b(?:opencohost|kira)(?:'s| s)? (?:repository|repo|source code|github)\b",
    ),
    TOPIC_INTERLOCUTOR: _rx(
        r"\bcomo me llamo\b", r"\bquien soy\b", r"\bcual es mi nombre\b",
        r"\bcon quien (?:hablas|estas hablando)\b",
        r"\bwho am i\b", r"\bwho i am\b", r"\bwhat is my name\b", r"\bmy name\b",
        r"\bwho are you talking to\b",
    ),
}


class SelfKnowledgeGate:
    """Pure decision object: ``detect(text)`` -> topics in stable order."""

    def detect(self, text: str) -> tuple[str, ...]:
        if not isinstance(text, str) or not text.strip():
            return ()
        norm = _normalize(text)
        return tuple(topic for topic in ALL_TOPICS if _RULES[topic].search(norm))
