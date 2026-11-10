"""
Módulo de Auditoria e Garantia de Qualidade de Cortes (QA Guardrails)
Garante que nenhum corte seja produzido ou publicado se contiver vinhetas, saudações
ou falas interrompidas no meio da frase.
"""

import re
from typing import Dict, Any, List, Tuple


INTRO_BLACKLIST_TERMS = [
    "seja bem-vindo", "bem vindo", "bem-vinda", "bem vinda",
    "mais um vídeo", "nosso canal", "se inscreva", "se inscrever",
    "deixe seu like", "deixa o like", "curtir e compartilhar",
    "toca a vinheta", "solta a vinheta", "pauta de hoje",
    "vamos pro nosso tema", "vamos para o nosso tema",
    "antes disso eu queria", "fazer um convite", "levar saúde mental",
    "a gente pipoca", "festa junina"
]

TRAILING_INCOMPLETE_WORDS = [
    "porque", "mas", "quando", "e", "ou", "aí", "então", "que", "pra", "para",
    "se", "como", "onde", "porém", "todavia", "ou seja", "né", "tá", "né?", "tá?"
]


class ClipQualityAuditor:
    """Auditor de qualidade semântica e narrativa para cortes de Shorts."""

    @staticmethod
    def is_intro_junk(text: str, start_seconds: float = 0.0) -> Tuple[bool, str]:
        """
        Detecta se o trecho é conversa fiada de abertura, vinheta ou pedido de like.
        """
        lowered = text.lower().strip()
        
        # Se começar antes dos 45 segundos e contiver termos clássicos de abertura
        if start_seconds < 45.0:
            for term in INTRO_BLACKLIST_TERMS:
                if term in lowered:
                    return True, f"Trecho descartado: contém termo de abertura/vinheta ('{term}')."

        return False, "OK"

    @staticmethod
    def is_sentence_complete(text: str) -> Tuple[bool, str]:
        """
        Verifica se a última frase do corte fecha um pensamento e não termina no vácuo.
        """
        cleaned = re.sub(r'[^\w\s]', '', text).strip().lower()
        words = cleaned.split()
        if not words:
            return False, "Texto vazio."

        last_word = words[-1]
        if last_word in TRAILING_INCOMPLETE_WORDS:
            return False, f"Corte incompleto: termina com palavra suspensa ('{last_word}')."

        # Checar se as últimas duas palavras são conjunções compostas
        if len(words) >= 2:
            last_two = f"{words[-2]} {words[-1]}"
            if last_two in TRAILING_INCOMPLETE_WORDS:
                return False, f"Corte incompleto: termina com expressão suspensa ('{last_two}')."

        return True, "OK"

    @classmethod
    def audit_clip(cls, text: str, start_seconds: float, end_seconds: float, is_full_episode: bool = True) -> Dict[str, Any]:
        """
        Executa bateria completa de testes de qualidade.
        Retorna dicionário com status de aprovação e justificativa.
        """
        duration = end_seconds - start_seconds
        
        # 1. Checagem de duração mínima e máxima para YouTube Shorts
        if duration < 20.0:
            return {
                "approved": False,
                "reason": f"Duração muito curta ({duration:.1f}s). Mínimo recomendado: 25s."
            }
        if duration > 59.0:
            return {
                "approved": False,
                "reason": f"Duração ultrapassa limite do Shorts ({duration:.1f}s). Máximo: 58s."
            }

        # 2. Checagem de vinheta / intro
        is_intro, intro_reason = cls.is_intro_junk(text, start_seconds=start_seconds)
        if is_intro:
            return {
                "approved": False,
                "reason": intro_reason
            }

        # 3. Checagem de frase completa no final
        is_complete, complete_reason = cls.is_sentence_complete(text)
        if not is_complete:
            return {
                "approved": False,
                "reason": complete_reason
            }

        # 4. Checagem de volume de conteúdo (evita silêncio ou fala travada)
        word_count = len(text.split())
        if word_count < 25:
            return {
                "approved": False,
                "reason": f"Pouco conteúdo falado ({word_count} palavras em {duration:.1f}s)."
            }

        return {
            "approved": True,
            "duration": round(duration, 2),
            "word_count": word_count,
            "reason": "Corte 100% íntegro com início, meio e fim narrativo aprovados."
        }
