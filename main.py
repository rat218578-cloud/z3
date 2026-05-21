"""
BAC BO BOT - ML EVOLUTION v9.1
CORREÇÃO: APRENDIZADO DO ZERO COM JANELA DESLIZANTE E INVERSÃO AUTOMÁTICA

Princípios:
1. ✅ USA JANELA DESLIZANTE (apenas dados recentes - últimas 20 ocorrências)
2. ✅ APRENDE DO ZERO com cada rodada (sem tabela estática)
3. ✅ INVERTE REGRAS que erram consistentemente (2 erros = inverte)
4. ✅ DESCARTA REGRAS ANTIGAS automaticamente (após 7 dias sem atualização)
5. ✅ TIE não é considerado erro (não afeta aprendizado)
6. ✅ Score Exato tem PRIORIDADE ABSOLUTA

O bot NÃO USA tabela estática da tese - aprende APENAS com dados reais em tempo real.

ALTERAÇÃO v9.2:
- API DIRETO (page=0&size=10&sort=data.settledAt,desc) é a fonte PRINCIPAL
- API LATEST é apenas FALLBACK (quando o direto trava)
"""

import os
import json
import time
import threading
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict, Any
from collections import deque, Counter
import logging

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
import psycopg2
import urllib.parse

# =============================================================================
# CONFIGURAÇÕES
# =============================================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://neondb_owner:npg_hW3kU9LZfsgB@ep-summer-meadow-ap9gu9vy-pooler.c-7.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require")
API_URL = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo"
LATEST_API_URL = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo/latest"
# ===== ALTERAÇÃO PRINCIPAL v9.2 =====
# API DIRETO é a fonte PRINCIPAL (com page e size)
API_DIRETO = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo?page=0&size=10&sort=data.settledAt,desc"
# ====================================
PORT = int(os.environ.get("PORT", 5000))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Cache-Control': 'no-cache'
}

HISTORICO_MAX = 500
ULTIMO_ID_CONTROLE = {}

# =============================================================================
# CONFIGURAÇÕES v9.1 - JANELA DESLIZANTE
# =============================================================================

# Confiança mínima para apostar (reduzido para permitir mais aprendizado)
CONFIANCA_MINIMA = 65

# Amostras mínimas na janela para considerar uma regra
AMOSTRAS_MINIMAS = 5

# Tamanho da janela deslizante (últimas N ocorrências por score)
JANELA_TAMANHO = 20

# Dias para manter regras aprendidas (regras antigas são descartadas)
DIAS_MANTER_REGRAS = 7

# Limite de erros consecutivos para ativar modo emergência
ERROS_CONSECUTIVOS_LIMITE = 5

# Número de erros consecutivos para inverter uma regra
ERROS_PARA_INVERSAO = 2

# Dias de dados recentes para carregar do banco
DIAS_RECENTES = 7


# =============================================================================
# Mapa de inversão
# =============================================================================

INVERSAO = {
    'PLAYER': 'BANKER',
    'BANKER': 'PLAYER'
}


# =============================================================================
# MODELOS DE DADOS
# =============================================================================

@dataclass
class Rodada:
    id: str
    data_hora: datetime
    player_score: int
    banker_score: int
    resultado: str
    fonte: str
    soma: int = 0
    
    def __post_init__(self):
        self.soma = self.player_score + self.banker_score


@dataclass
class Decisao:
    apostar: bool
    previsao: Optional[str]
    regra: str
    confianca: int
    nivel: int
    player_score: int
    banker_score: int
    soma: int
    timestamp: datetime
    foi_invertida: bool = False
    regra_original: Optional[str] = None


@dataclass
class RegraScoreJanela:
    """
    Regra de score exato com JANELA DESLIZANTE e INVERSÃO AUTOMÁTICA
    - Aprende APENAS com os últimos N resultados (JANELA_TAMANHO)
    - Regras antigas são naturalmente descartadas
    - Inversão automática quando erra consistentemente
    """
    player_score: int
    banker_score: int
    previsao: str
    confianca: int
    total_ocorrencias: int
    acertos: int
    erros: int
    inversoes: int
    streak_erros: int = 0
    ultimo_resultado: Optional[str] = None
    janela_resultados: deque = field(default_factory=lambda: deque(maxlen=JANELA_TAMANHO))
    criada_em: datetime = field(default_factory=datetime.now)
    atualizada_em: datetime = field(default_factory=datetime.now)
    
    @property
    def taxa_acerto(self) -> float:
        if self.total_ocorrencias == 0:
            return 0
        # Confiança baseada APENAS na janela deslizante
        if len(self.janela_resultados) > 0:
            acertos_janela = sum(1 for r in self.janela_resultados if r)
            return (acertos_janela / len(self.janela_resultados)) * 100
        return (self.acertos / self.total_ocorrencias) * 100
    
    @property
    def precisa_inverter(self) -> bool:
        """Verifica se a regra precisa ser invertida baseado nos erros consecutivos"""
        return self.streak_erros >= ERROS_PARA_INVERSAO
    
    def inverter(self):
        """Inverte a previsão da regra"""
        self.previsao = INVERSAO[self.previsao]
        self.inversoes += 1
        self.streak_erros = 0
        self.atualizada_em = datetime.now()
        logger.warning(f"🔄 REGRA INVERTIDA: {self.player_score}vs{self.banker_score} → nova previsão: {self.previsao} (inversões: {self.inversoes})")
    
    def registrar_acerto(self):
        self.acertos += 1
        self.total_ocorrencias += 1
        self.streak_erros = 0
        self.janela_resultados.append(True)
        self.atualizada_em = datetime.now()
    
    def registrar_erro(self):
        self.erros += 1
        self.total_ocorrencias += 1
        self.streak_erros += 1
        self.janela_resultados.append(False)
        self.atualizada_em = datetime.now()
        
        if self.precisa_inverter:
            self.inverter()
    
    def to_dict(self) -> Dict:
        return {
            'player': self.player_score,
            'banker': self.banker_score,
            'previsao': self.previsao,
            'confianca': int(self.taxa_acerto),
            'total_ocorrencias': self.total_ocorrencias,
            'acertos': self.acertos,
            'erros': self.erros,
            'taxa_acerto': round(self.taxa_acerto, 1),
            'inversoes': self.inversoes,
            'streak_erros': self.streak_erros,
            'janela_tamanho': len(self.janela_resultados),
            'criada_em': self.criada_em.isoformat() if self.criada_em else None,
            'atualizada_em': self.atualizada_em.isoformat() if self.atualizada_em else None
        }


# =============================================================================
# DATABASE MANAGER COM POSTGRESQL
# =============================================================================

class DatabaseManager:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self._init_tables()
    
    def _get_connection(self):
        if not self.database_url:
            return None
        try:
            parsed = urllib.parse.urlparse(self.database_url)
            conn = psycopg2.connect(
                host=parsed.hostname,
                port=parsed.port or 5432,
                user=parsed.username,
                password=parsed.password,
                database=parsed.path[1:],
                sslmode='require',
                connect_timeout=10
            )
            return conn
        except Exception as e:
            logger.error(f"Erro conectar: {e}")
            return None
    
    def _init_tables(self):
        conn = self._get_connection()
        if not conn:
            logger.warning("⚠️ Sem conexão com banco, usando memória")
            return
        try:
            cur = conn.cursor()
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rodadas (
                    id VARCHAR(50) PRIMARY KEY,
                    data_hora TIMESTAMP NOT NULL,
                    player_score INT NOT NULL,
                    banker_score INT NOT NULL,
                    resultado VARCHAR(10) NOT NULL,
                    soma INT NOT NULL,
                    fonte VARCHAR(20),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS decisoes (
                    id SERIAL PRIMARY KEY,
                    rodada_id VARCHAR(50) REFERENCES rodadas(id),
                    apostar BOOLEAN NOT NULL,
                    previsao VARCHAR(10),
                    regra VARCHAR(100),
                    confianca INT,
                    nivel INT,
                    player_score INT,
                    banker_score INT,
                    soma INT,
                    acertou BOOLEAN,
                    foi_invertida BOOLEAN DEFAULT FALSE,
                    regra_original VARCHAR(100),
                    timestamp TIMESTAMP DEFAULT NOW()
                )
            """)
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS evolucao (
                    id SERIAL PRIMARY KEY,
                    periodo INT NOT NULL,
                    total_apostas INT NOT NULL,
                    total_acertos INT NOT NULL,
                    precisao DECIMAL(5,2),
                    limiar INT,
                    timestamp TIMESTAMP DEFAULT NOW()
                )
            """)
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS erros (
                    id SERIAL PRIMARY KEY,
                    rodada_id VARCHAR(50) REFERENCES rodadas(id),
                    decisao_id INT REFERENCES decisoes(id),
                    previsao VARCHAR(10) NOT NULL,
                    resultado VARCHAR(10) NOT NULL,
                    confianca INT NOT NULL,
                    regra VARCHAR(100),
                    streak INT DEFAULT 0,
                    soma_atual INT DEFAULT 0,
                    ultimo_resultado VARCHAR(10),
                    padrao VARCHAR(50),
                    foi_invertida BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS analise_padroes (
                    id SERIAL PRIMARY KEY,
                    padrao VARCHAR(50) NOT NULL,
                    total_ocorrencias INT DEFAULT 0,
                    total_erros INT DEFAULT 0,
                    total_acertos INT DEFAULT 0,
                    taxa_erro DECIMAL(5,2) DEFAULT 0,
                    ultima_ocorrencia TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS regras_aprendidas (
                    id SERIAL PRIMARY KEY,
                    player_score INT NOT NULL,
                    banker_score INT NOT NULL,
                    previsao VARCHAR(10) NOT NULL,
                    confianca INT NOT NULL,
                    total_ocorrencias INT NOT NULL,
                    acertos INT NOT NULL,
                    erros INT DEFAULT 0,
                    inversoes INT DEFAULT 0,
                    streak_erros INT DEFAULT 0,
                    janela_resultados TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(player_score, banker_score)
                )
            """)
            
            cur.execute("CREATE INDEX IF NOT EXISTS idx_erros_padrao ON erros(padrao)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_erros_confianca ON erros(confianca)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_erros_created_at ON erros(created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_analise_padroes_padrao ON analise_padroes(padrao)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_regras_score ON regras_aprendidas(player_score, banker_score)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_regras_inversoes ON regras_aprendidas(inversoes)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_regras_updated ON regras_aprendidas(updated_at)")
            
            conn.commit()
            cur.close()
            logger.info("✅ Tabelas criadas/verificadas (v9.1 - Janela Deslizante)")
        except Exception as e:
            logger.error(f"Erro tabelas: {e}")
        finally:
            conn.close()
    
    def salvar_regra_aprendida(self, regra: RegraScoreJanela) -> bool:
        conn = self._get_connection()
        if not conn:
            return False
        try:
            cur = conn.cursor()
            # Serializa a janela de resultados para JSON
            janela_str = json.dumps(list(regra.janela_resultados))
            
            cur.execute("""
                INSERT INTO regras_aprendidas (player_score, banker_score, previsao, confianca, 
                                               total_ocorrencias, acertos, erros, inversoes, streak_erros,
                                               janela_resultados, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (player_score, banker_score) DO UPDATE SET
                    previsao = EXCLUDED.previsao,
                    confianca = EXCLUDED.confianca,
                    total_ocorrencias = EXCLUDED.total_ocorrencias,
                    acertos = EXCLUDED.acertos,
                    erros = EXCLUDED.erros,
                    inversoes = EXCLUDED.inversoes,
                    streak_erros = EXCLUDED.streak_erros,
                    janela_resultados = EXCLUDED.janela_resultados,
                    updated_at = NOW()
            """, (regra.player_score, regra.banker_score, regra.previsao, int(regra.taxa_acerto),
                  regra.total_ocorrencias, regra.acertos, regra.erros, regra.inversoes, regra.streak_erros,
                  janela_str, regra.criada_em, regra.atualizada_em))
            conn.commit()
            cur.close()
            return True
        except Exception as e:
            logger.error(f"Erro salvar regra: {e}")
            return False
        finally:
            conn.close()
    
    def get_regras_aprendidas(self, min_confianca: int = 0, dias_recentes: int = DIAS_RECENTES) -> List[Dict]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT player_score, banker_score, previsao, confianca, 
                       total_ocorrencias, acertos, erros, inversoes, streak_erros,
                       janela_resultados, created_at, updated_at
                FROM regras_aprendidas
                WHERE confianca >= %s 
                    AND updated_at > NOW() - INTERVAL '%s days'
                ORDER BY confianca DESC, total_ocorrencias DESC
            """, (min_confianca, dias_recentes))
            rows = cur.fetchall()
            cur.close()
            
            regras = []
            for r in rows:
                janela = []
                if r[9]:
                    try:
                        janela = json.loads(r[9])
                    except:
                        pass
                
                regras.append({
                    'player': r[0],
                    'banker': r[1],
                    'previsao': r[2],
                    'confianca': r[3],
                    'total_ocorrencias': r[4],
                    'acertos': r[5],
                    'erros': r[6],
                    'inversoes': r[7],
                    'streak_erros': r[8],
                    'janela_tamanho': len(janela),
                    'created_at': r[10].isoformat() if r[10] else None,
                    'updated_at': r[11].isoformat() if r[11] else None
                })
            return regras
        except Exception as e:
            logger.error(f"Erro get_regras: {e}")
            return []
        finally:
            conn.close()
    
    def get_regra_especifica(self, player_score: int, banker_score: int) -> Optional[Dict]:
        conn = self._get_connection()
        if not conn:
            return None
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT player_score, banker_score, previsao, confianca, 
                       total_ocorrencias, acertos, erros, inversoes, streak_erros,
                       janela_resultados
                FROM regras_aprendidas
                WHERE player_score = %s AND banker_score = %s
            """, (player_score, banker_score))
            row = cur.fetchone()
            cur.close()
            if row:
                janela = []
                if row[9]:
                    try:
                        janela = json.loads(row[9])
                    except:
                        pass
                
                return {
                    'player': row[0],
                    'banker': row[1],
                    'previsao': row[2],
                    'confianca': row[3],
                    'total_ocorrencias': row[4],
                    'acertos': row[5],
                    'erros': row[6],
                    'inversoes': row[7],
                    'streak_erros': row[8],
                    'janela_tamanho': len(janela)
                }
            return None
        except Exception as e:
            return None
        finally:
            conn.close()
    
    def deletar_regras_antigas(self, dias: int = DIAS_MANTER_REGRAS) -> int:
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM regras_aprendidas
                WHERE updated_at < NOW() - INTERVAL '%s days'
                RETURNING id
            """, (dias,))
            deletadas = cur.rowcount
            conn.commit()
            cur.close()
            if deletadas > 0:
                logger.info(f"🗑️ {deletadas} regras antigas (> {dias} dias) removidas")
            return deletadas
        except Exception as e:
            logger.error(f"Erro deletar regras: {e}")
            return 0
        finally:
            conn.close()
    
    def get_total_erros(self) -> int:
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM erros")
            total = cur.fetchone()[0]
            cur.close()
            return total
        except Exception as e:
            return 0
        finally:
            conn.close()
    
    def get_ultimos_erros(self, limit: int = 20, offset: int = 0) -> List[Dict]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT e.id, e.previsao, e.resultado, e.confianca, e.regra, 
                       e.streak, e.soma_atual, e.padrao, e.created_at, e.foi_invertida,
                       r.player_score, r.banker_score, r.soma
                FROM erros e
                LEFT JOIN rodadas r ON e.rodada_id = r.id
                ORDER BY e.created_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
            rows = cur.fetchall()
            cur.close()
            return [{
                'id': r[0],
                'previsao': r[1],
                'resultado': r[2],
                'confianca': r[3],
                'regra': r[4],
                'streak': r[5],
                'soma_atual': r[6],
                'padrao': r[7] or 'N/A',
                'created_at': r[8].isoformat() if r[8] else None,
                'foi_invertida': r[9],
                'player_score': r[10],
                'banker_score': r[11],
                'soma': r[12]
            } for r in rows]
        except Exception as e:
            return []
        finally:
            conn.close()
    
    def get_estatisticas_erros(self) -> Dict:
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM erros")
            total_erros = cur.fetchone()[0]
            
            cur.execute("""
                SELECT confianca, COUNT(*) as total
                FROM erros
                GROUP BY confianca
                ORDER BY total DESC
                LIMIT 5
            """)
            erros_por_confianca = [{'confianca': r[0], 'total': r[1]} for r in cur.fetchall()]
            
            cur.execute("""
                SELECT regra, COUNT(*) as total
                FROM erros
                WHERE regra IS NOT NULL
                GROUP BY regra
                ORDER BY total DESC
                LIMIT 5
            """)
            erros_por_regra = [{'regra': r[0], 'total': r[1]} for r in cur.fetchall()]
            
            cur.execute("""
                SELECT padrao, COUNT(*) as total
                FROM erros
                WHERE padrao IS NOT NULL
                GROUP BY padrao
                ORDER BY total DESC
                LIMIT 5
            """)
            erros_por_padrao = [{'padrao': r[0], 'total': r[1]} for r in cur.fetchall()]
            
            cur.execute("SELECT COUNT(*) FROM erros WHERE foi_invertida = TRUE")
            erros_invertidos = cur.fetchone()[0]
            
            cur.close()
            
            return {
                'total_erros': total_erros,
                'erros_invertidos': erros_invertidos,
                'erros_por_confianca': erros_por_confianca,
                'erros_por_regra': erros_por_regra,
                'erros_por_padrao': erros_por_padrao
            }
        except Exception as e:
            return {}
        finally:
            conn.close()
    
    def get_piores_padroes(self, limit: int = 10, min_ocorrencias: int = 3) -> List[Dict]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT padrao, total_ocorrencias, total_erros, total_acertos, taxa_erro, ultima_ocorrencia
                FROM analise_padroes
                WHERE total_ocorrencias >= %s
                ORDER BY taxa_erro DESC
                LIMIT %s
            """, (min_ocorrencias, limit))
            rows = cur.fetchall()
            cur.close()
            return [{
                'padrao': r[0] or 'N/A',
                'total_ocorrencias': r[1],
                'total_erros': r[2],
                'total_acertos': r[3],
                'taxa_erro': float(r[4]) if r[4] else 0,
                'ultima_ocorrencia': r[5].isoformat() if r[5] else None
            } for r in rows]
        except Exception as e:
            return []
        finally:
            conn.close()
    
    def get_melhores_padroes(self, limit: int = 10, min_ocorrencias: int = 3) -> List[Dict]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT padrao, total_ocorrencias, total_erros, total_acertos, taxa_erro, ultima_ocorrencia
                FROM analise_padroes
                WHERE total_ocorrencias >= %s
                ORDER BY taxa_erro ASC
                LIMIT %s
            """, (min_ocorrencias, limit))
            rows = cur.fetchall()
            cur.close()
            return [{
                'padrao': r[0] or 'N/A',
                'total_ocorrencias': r[1],
                'total_erros': r[2],
                'total_acertos': r[3],
                'taxa_erro': float(r[4]) if r[4] else 0,
                'ultima_ocorrencia': r[5].isoformat() if r[5] else None
            } for r in rows]
        except Exception as e:
            return []
        finally:
            conn.close()
    
    def get_erros_por_confianca(self, min_amostras: int = 3) -> List[Dict]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT 
                    CASE 
                        WHEN confianca BETWEEN 65 AND 69 THEN '65-69'
                        WHEN confianca BETWEEN 70 AND 74 THEN '70-74'
                        WHEN confianca BETWEEN 75 AND 79 THEN '75-79'
                        WHEN confianca BETWEEN 80 AND 84 THEN '80-84'
                        WHEN confianca BETWEEN 85 AND 89 THEN '85-89'
                        WHEN confianca BETWEEN 90 AND 94 THEN '90-94'
                        ELSE '95-100'
                    END as faixa,
                    COUNT(*) as total_erros,
                    AVG(confianca) as confianca_media
                FROM erros
                GROUP BY faixa
            """)
            rows = cur.fetchall()
            cur.close()
            
            resultados = []
            for row in rows:
                faixa = row[0]
                total_erros = row[1]
                conf_media = row[2]
                
                cur2 = conn.cursor()
                if faixa == '65-69':
                    cur2.execute("SELECT COUNT(*) FROM decisoes WHERE apostar = TRUE AND confianca BETWEEN 65 AND 69")
                elif faixa == '70-74':
                    cur2.execute("SELECT COUNT(*) FROM decisoes WHERE apostar = TRUE AND confianca BETWEEN 70 AND 74")
                elif faixa == '75-79':
                    cur2.execute("SELECT COUNT(*) FROM decisoes WHERE apostar = TRUE AND confianca BETWEEN 75 AND 79")
                elif faixa == '80-84':
                    cur2.execute("SELECT COUNT(*) FROM decisoes WHERE apostar = TRUE AND confianca BETWEEN 80 AND 84")
                elif faixa == '85-89':
                    cur2.execute("SELECT COUNT(*) FROM decisoes WHERE apostar = TRUE AND confianca BETWEEN 85 AND 89")
                elif faixa == '90-94':
                    cur2.execute("SELECT COUNT(*) FROM decisoes WHERE apostar = TRUE AND confianca BETWEEN 90 AND 94")
                else:
                    cur2.execute("SELECT COUNT(*) FROM decisoes WHERE apostar = TRUE AND confianca >= 95")
                
                total_apostas = cur2.fetchone()[0]
                cur2.close()
                
                if total_apostas >= min_amostras:
                    taxa_erro = (total_erros / max(1, total_apostas)) * 100
                    resultados.append({
                        'faixa': faixa,
                        'total_apostas': total_apostas,
                        'total_erros': total_erros,
                        'taxa_erro': round(taxa_erro, 2),
                        'confianca_media': round(conf_media, 1) if conf_media else 0
                    })
            
            return resultados
        except Exception as e:
            return []
        finally:
            conn.close()
    
    def get_erros_por_regra(self, min_amostras: int = 3) -> List[Dict]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT 
                    regra,
                    COUNT(*) as total_erros,
                    AVG(confianca) as confianca_media,
                    SUM(CASE WHEN foi_invertida THEN 1 ELSE 0 END) as erros_invertidos
                FROM erros
                WHERE regra IS NOT NULL
                GROUP BY regra
                HAVING COUNT(*) >= %s
                ORDER BY total_erros DESC
            """, (min_amostras,))
            rows = cur.fetchall()
            cur.close()
            
            resultados = []
            for row in rows:
                regra = row[0]
                total_erros = row[1]
                conf_media = row[2]
                erros_invertidos = row[3]
                
                cur2 = conn.cursor()
                cur2.execute("SELECT COUNT(*) FROM decisoes WHERE apostar = TRUE AND regra = %s", (regra,))
                total_apostas = cur2.fetchone()[0]
                cur2.close()
                
                cur3 = conn.cursor()
                cur3.execute("SELECT COUNT(*) FROM decisoes WHERE apostar = TRUE AND regra = %s AND acertou = TRUE", (regra,))
                total_acertos = cur3.fetchone()[0]
                cur3.close()
                
                taxa_acerto = (total_acertos / max(1, total_apostas)) * 100
                
                resultados.append({
                    'regra': regra,
                    'total_apostas': total_apostas,
                    'total_acertos': total_acertos,
                    'total_erros': total_erros,
                    'erros_invertidos': erros_invertidos,
                    'taxa_acerto': round(taxa_acerto, 2),
                    'confianca_media': round(conf_media, 1) if conf_media else 0
                })
            
            return resultados
        except Exception as e:
            return []
        finally:
            conn.close()
    
    def get_erros_por_streak(self) -> List[Dict]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT streak, COUNT(*) as total_erros
                FROM erros
                WHERE streak > 0
                GROUP BY streak
                ORDER BY streak
            """)
            rows = cur.fetchall()
            cur.close()
            return [{'streak': r[0], 'total_erros': r[1]} for r in rows]
        except Exception as e:
            return []
        finally:
            conn.close()
    
    def get_erros_por_soma(self, min_amostras: int = 3) -> List[Dict]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT 
                    CASE 
                        WHEN soma_atual <= 6 THEN 'baixa (2-6)'
                        WHEN soma_atual BETWEEN 7 AND 10 THEN 'media (7-10)'
                        WHEN soma_atual BETWEEN 11 AND 14 THEN 'media-alta (11-14)'
                        WHEN soma_atual BETWEEN 15 AND 18 THEN 'alta (15-18)'
                        ELSE 'muito alta (19-24)'
                    END as faixa_soma,
                    COUNT(*) as total_erros
                FROM erros
                WHERE soma_atual > 0
                GROUP BY faixa_soma
                HAVING COUNT(*) >= %s
                ORDER BY faixa_soma
            """, (min_amostras,))
            rows = cur.fetchall()
            cur.close()
            return [{'faixa_soma': r[0], 'total_erros': r[1]} for r in rows]
        except Exception as e:
            return []
        finally:
            conn.close()
    
    def get_erro_detalhado(self, erro_id: int) -> Optional[Dict]:
        conn = self._get_connection()
        if not conn:
            return None
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT e.id, e.previsao, e.resultado, e.confianca, e.regra, 
                       e.streak, e.soma_atual, e.padrao, e.created_at, e.foi_invertida,
                       r.id as rodada_id, r.player_score, r.banker_score, r.soma, r.data_hora,
                       d.id as decisao_id, d.timestamp as decisao_timestamp
                FROM erros e
                LEFT JOIN rodadas r ON e.rodada_id = r.id
                LEFT JOIN decisoes d ON e.decisao_id = d.id
                WHERE e.id = %s
            """, (erro_id,))
            row = cur.fetchone()
            cur.close()
            
            if not row:
                return None
            
            return {
                'id': row[0],
                'previsao': row[1],
                'resultado': row[2],
                'confianca': row[3],
                'regra': row[4],
                'streak': row[5],
                'soma_atual': row[6],
                'padrao': row[7],
                'created_at': row[8].isoformat() if row[8] else None,
                'foi_invertida': row[9],
                'rodada': {
                    'id': row[10],
                    'player_score': row[11],
                    'banker_score': row[12],
                    'soma': row[13],
                    'data_hora': row[14].isoformat() if row[14] else None
                },
                'decisao': {
                    'id': row[15],
                    'timestamp': row[16].isoformat() if row[16] else None
                }
            }
        except Exception as e:
            return None
        finally:
            conn.close()
    
    def salvar_erro(self, rodada_id: str, decisao_id: int, previsao: str, resultado: str, 
                    confianca: int, regra: str, streak: int, soma_atual: int, 
                    ultimo_resultado: str, padrao: str, foi_invertida: bool = False) -> bool:
        conn = self._get_connection()
        if not conn:
            return False
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO erros (rodada_id, decisao_id, previsao, resultado, confianca, 
                                   regra, streak, soma_atual, ultimo_resultado, padrao, foi_invertida)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (rodada_id, decisao_id, previsao, resultado, confianca, regra, 
                  streak, soma_atual, ultimo_resultado, padrao, foi_invertida))
            conn.commit()
            cur.close()
            
            self._atualizar_analise_padrao(padrao, False)
            
            return True
        except Exception as e:
            logger.error(f"Erro salvar erro: {e}")
            return False
        finally:
            conn.close()
    
    def salvar_acerto_para_analise(self, padrao: str) -> bool:
        if not padrao:
            return False
        return self._atualizar_analise_padrao(padrao, True)
    
    def _atualizar_analise_padrao(self, padrao: str, foi_acerto: bool) -> bool:
        if not padrao:
            return False
            
        conn = self._get_connection()
        if not conn:
            return False
        try:
            cur = conn.cursor()
            
            cur.execute("SELECT id, total_erros, total_acertos FROM analise_padroes WHERE padrao = %s", (padrao,))
            existing = cur.fetchone()
            
            if existing:
                if foi_acerto:
                    cur.execute("""
                        UPDATE analise_padroes 
                        SET total_acertos = total_acertos + 1,
                            total_ocorrencias = total_ocorrencias + 1,
                            taxa_erro = (total_erros * 100.0 / NULLIF(total_erros + total_acertos + 1, 0)),
                            updated_at = NOW(),
                            ultima_ocorrencia = NOW()
                        WHERE padrao = %s
                    """, (padrao,))
                else:
                    cur.execute("""
                        UPDATE analise_padroes 
                        SET total_erros = total_erros + 1,
                            total_ocorrencias = total_ocorrencias + 1,
                            taxa_erro = ((total_erros + 1) * 100.0 / NULLIF(total_erros + total_acertos + 1, 0)),
                            updated_at = NOW(),
                            ultima_ocorrencia = NOW()
                        WHERE padrao = %s
                    """, (padrao,))
            else:
                if foi_acerto:
                    cur.execute("""
                        INSERT INTO analise_padroes (padrao, total_ocorrencias, total_acertos, taxa_erro, ultima_ocorrencia)
                        VALUES (%s, 1, 1, 0, NOW())
                    """, (padrao,))
                else:
                    cur.execute("""
                        INSERT INTO analise_padroes (padrao, total_ocorrencias, total_erros, taxa_erro, ultima_ocorrencia)
                        VALUES (%s, 1, 1, 100, NOW())
                    """, (padrao,))
            
            conn.commit()
            cur.close()
            return True
        except Exception as e:
            logger.error(f"Erro atualizar padrao: {e}")
            return False
        finally:
            conn.close()
    
    def salvar_rodada(self, rodada: Rodada) -> bool:
        conn = self._get_connection()
        if not conn:
            return False
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO rodadas (id, data_hora, player_score, banker_score, resultado, soma, fonte)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (rodada.id, rodada.data_hora, rodada.player_score, rodada.banker_score,
                  rodada.resultado, rodada.soma, rodada.fonte))
            conn.commit()
            cur.close()
            return True
        except Exception as e:
            logger.error(f"Erro salvar rodada: {e}")
            return False
        finally:
            conn.close()
    
    def salvar_decisao(self, decisao: Decisao, rodada_id: str = None) -> Tuple[bool, Optional[int]]:
        conn = self._get_connection()
        if not conn:
            return False, None
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO decisoes (rodada_id, apostar, previsao, regra, confianca, nivel, 
                                      player_score, banker_score, soma, foi_invertida, regra_original)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (rodada_id, decisao.apostar, decisao.previsao, decisao.regra,
                  decisao.confianca, decisao.nivel, decisao.player_score,
                  decisao.banker_score, decisao.soma, decisao.foi_invertida, decisao.regra_original))
            decisao_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
            return True, decisao_id
        except Exception as e:
            logger.error(f"Erro salvar decisao: {e}")
            return False, None
        finally:
            conn.close()
    
    def atualizar_acerto(self, rodada_id: str, acertou: bool, decisao_id: int = None):
        conn = self._get_connection()
        if not conn:
            return
        try:
            cur = conn.cursor()
            if decisao_id:
                cur.execute("UPDATE decisoes SET acertou = %s WHERE id = %s", (acertou, decisao_id))
            else:
                cur.execute("UPDATE decisoes SET acertou = %s WHERE rodada_id = %s AND apostar = TRUE", (acertou, rodada_id))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"Erro atualizar acerto: {e}")
        finally:
            conn.close()
    
    def salvar_evolucao(self, periodo: int, total_apostas: int, total_acertos: int, precisao: float, limiar: int):
        conn = self._get_connection()
        if not conn:
            return
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO evolucao (periodo, total_apostas, total_acertos, precisao, limiar)
                VALUES (%s, %s, %s, %s, %s)
            """, (periodo, total_apostas, total_acertos, precisao, limiar))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"Erro salvar evolucao: {e}")
        finally:
            conn.close()
    
    def get_historico(self, limit: int = 900) -> List[Rodada]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, data_hora, player_score, banker_score, resultado, fonte
                FROM rodadas
                ORDER BY data_hora DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cur.close()
            historico = []
            for row in rows:
                rodada = Rodada(
                    id=row[0], data_hora=row[1], player_score=row[2],
                    banker_score=row[3], resultado=row[4], fonte=row[5] or 'historico'
                )
                historico.append(rodada)
            return historico
        except Exception as e:
            logger.error(f"Erro get_historico: {e}")
            return []
        finally:
            conn.close()
    
    def get_rodadas_limit(self, limit: int = 50) -> List[dict]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT data_hora, player_score, banker_score, resultado, soma
                FROM rodadas
                ORDER BY data_hora DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cur.close()
            rodadas = []
            for row in rows:
                rodadas.append({
                    'data': row[0].strftime('%H:%M:%S'),
                    'player': row[1],
                    'banker': row[2],
                    'resultado': row[3],
                    'soma': row[4]
                })
            return rodadas
        except Exception as e:
            logger.error(f"Erro get_rodadas_limit: {e}")
            return []
        finally:
            conn.close()
    
    def get_total_rodadas(self) -> int:
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM rodadas")
            count = cur.fetchone()[0]
            cur.close()
            return count
        except Exception as e:
            return 0
        finally:
            conn.close()
    
    def get_estatisticas(self) -> Tuple[int, int, float]:
        conn = self._get_connection()
        if not conn:
            return (0, 0, 0.0)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN acertou THEN 1 ELSE 0 END) as acertos,
                       ROUND(COALESCE(SUM(CASE WHEN acertou THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 0), 2) as precisao
                FROM decisoes WHERE apostar = TRUE
            """)
            row = cur.fetchone()
            cur.close()
            return (row[0] or 0, row[1] or 0, row[2] or 0.0)
        except Exception as e:
            return (0, 0, 0.0)
        finally:
            conn.close()
    
    def get_historico_apostas(self, limit: int = 30) -> List[dict]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT timestamp, previsao, regra, confianca, acertou, player_score, banker_score, foi_invertida
                FROM decisoes WHERE apostar = TRUE ORDER BY timestamp DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cur.close()
            historico = []
            for row in rows:
                historico.append({
                    'data': row[0].strftime('%d/%m %H:%M:%S'),
                    'previsao': row[1],
                    'regra': row[2],
                    'confianca': row[3],
                    'acertou': row[4],
                    'player': row[5],
                    'banker': row[6],
                    'foi_invertida': row[7]
                })
            return historico
        except Exception as e:
            return []
        finally:
            conn.close()
    
    def get_evolucao(self, limit: int = 50) -> List[dict]:
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT periodo, total_apostas, total_acertos, precisao, limiar, timestamp
                FROM evolucao ORDER BY periodo ASC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cur.close()
            return [{'periodo': r[0], 'total_apostas': r[1], 'total_acertos': r[2], 
                     'precisao': r[3], 'limiar': r[4], 'timestamp': r[5].isoformat()} for r in rows]
        except Exception as e:
            return []
        finally:
            conn.close()


# =============================================================================
# GERENCIADOR DE REGRAS COM JANELA DESLIZANTE E INVERSÃO AUTOMÁTICA
# =============================================================================

class GerenciadorRegrasJanela:
    """
    Gerencia as regras de score exato com JANELA DESLIZANTE e INVERSÃO AUTOMÁTICA
    
    Características:
    - NÃO USA TABELA ESTÁTICA - aprende do zero
    - Mantém apenas as últimas JANELA_TAMANHO ocorrências por score
    - Inverte regras automaticamente após ERROS_PARA_INVERSAO erros consecutivos
    - Descarta regras antigas automaticamente (DIAS_MANTER_REGRAS)
    """
    
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.regras: Dict[Tuple[int, int], RegraScoreJanela] = {}
        self.ultima_atualizacao = None
        self.confianca_minima = CONFIANCA_MINIMA
        self.minimo_amostras = AMOSTRAS_MINIMAS
        self.janela_tamanho = JANELA_TAMANHO
        
        # Carrega regras salvas do banco (apenas das últimas DIAS_RECENTES)
        self._carregar_regras_banco()
    
    def _carregar_regras_banco(self):
        """Carrega regras salvas do banco (apenas dias recentes)"""
        regras_db = self.db.get_regras_aprendidas(0, DIAS_RECENTES)
        
        for regra in regras_db:
            chave = (regra['player'], regra['banker'])
            self.regras[chave] = RegraScoreJanela(
                player_score=regra['player'],
                banker_score=regra['banker'],
                previsao=regra['previsao'],
                confianca=regra['confianca'],
                total_ocorrencias=regra['total_ocorrencias'],
                acertos=regra['acertos'],
                erros=regra.get('erros', 0),
                inversoes=regra.get('inversoes', 0),
                streak_erros=regra.get('streak_erros', 0)
            )
        
        if regras_db:
            logger.info(f"📚 Regras do banco carregadas: {len(regras_db)} regras (últimos {DIAS_RECENTES} dias)")
        else:
            logger.info("📚 Nenhuma regra existente no banco. O bot vai aprender do zero!")
    
    def prever(self, player_score: int, banker_score: int) -> Tuple[Optional[str], int, Optional[RegraScoreJanela]]:
        """Faz previsão baseada no score exato usando JANELA DESLIZANTE"""
        chave = (player_score, banker_score)
        
        if chave in self.regras:
            regra = self.regras[chave]
            # Confiança baseada na taxa de acerto da janela deslizante
            confianca_dinamica = int(regra.taxa_acerto)
            
            # Só aposta se tiver amostras suficientes na janela
            if len(regra.janela_resultados) >= self.minimo_amostras and confianca_dinamica >= self.confianca_minima:
                return regra.previsao, confianca_dinamica, regra
        
        return None, 0, None
    
    def registrar_resultado(self, player_score: int, banker_score: int, 
                           previsao_feita: str, resultado: str, acertou: bool,
                           regra_original: str = None) -> Optional[RegraScoreJanela]:
        """
        Registra o resultado real e atualiza a regra na JANELA DESLIZANTE
        - Mantém apenas as últimas JANELA_TAMANHO ocorrências
        - Inverte automaticamente se erra consistentemente
        """
        chave = (player_score, banker_score)
        
        # Se a regra não existe, cria uma nova (aprende do zero)
        if chave not in self.regras:
            # Busca no banco primeiro
            regra_db = self.db.get_regra_especifica(player_score, banker_score)
            if regra_db:
                self.regras[chave] = RegraScoreJanela(
                    player_score=player_score,
                    banker_score=banker_score,
                    previsao=regra_db['previsao'],
                    confianca=regra_db['confianca'],
                    total_ocorrencias=regra_db['total_ocorrencias'],
                    acertos=regra_db['acertos'],
                    erros=regra_db.get('erros', 0),
                    inversoes=regra_db.get('inversoes', 0),
                    streak_erros=regra_db.get('streak_erros', 0)
                )
            else:
                # Cria regra nova baseada no resultado (aprende do zero)
                self.regras[chave] = RegraScoreJanela(
                    player_score=player_score,
                    banker_score=banker_score,
                    previsao=resultado,
                    confianca=100,
                    total_ocorrencias=1,
                    acertos=1 if acertou else 0,
                    erros=0 if acertou else 1,
                    inversoes=0,
                    streak_erros=0 if acertou else 1
                )
                logger.info(f"🆕 Nova regra criada: {player_score}vs{banker_score} → {resultado}")
        
        regra = self.regras[chave]
        
        # Atualiza a regra baseado no resultado REAL
        if resultado != 'TIE':
            # Verifica se a previsão atual está correta
            previsao_atual_correta = (regra.previsao == resultado)
            
            if previsao_atual_correta:
                regra.registrar_acerto()
                logger.debug(f"✅ Regra {player_score}vs{banker_score}: acerto! ({regra.previsao}) | Janela: {len(regra.janela_resultados)}/{JANELA_TAMANHO}")
            else:
                regra.registrar_erro()
                logger.warning(f"❌ Regra {player_score}vs{banker_score}: ERRO! Previsão={regra.previsao}, Resultado={resultado} | Streak erros: {regra.streak_erros}")
                
                if regra.precisa_inverter:
                    logger.warning(f"🔄 Regra {player_score}vs{banker_score} INVERTIDA após {ERROS_PARA_INVERSAO} erros consecutivos!")
        
        # Salva no banco
        self.db.salvar_regra_aprendida(regra)
        
        return regra
    
    def get_regras_para_frontend(self, limit: int = 30) -> List[Dict]:
        regras_lista = []
        
        # Ordena por confiança (taxa de acerto)
        regras_ordenadas = sorted(
            self.regras.values(),
            key=lambda r: r.taxa_acerto,
            reverse=True
        )
        
        for regra in regras_ordenadas[:limit]:
            regras_lista.append(regra.to_dict())
        
        return regras_lista
    
    def get_regras_invertidas(self) -> List[Dict]:
        """Retorna apenas as regras que foram invertidas"""
        invertidas = []
        for regra in self.regras.values():
            if regra.inversoes > 0:
                invertidas.append(regra.to_dict())
        return invertidas
    
    def get_estatisticas(self) -> Dict:
        total_regras = len(self.regras)
        regras_ativas = sum(1 for r in self.regras.values() if len(r.janela_resultados) >= self.minimo_amostras and r.taxa_acerto >= self.confianca_minima)
        regras_aprendendo = sum(1 for r in self.regras.values() if 0 < len(r.janela_resultados) < self.minimo_amostras)
        total_inversoes = sum(r.inversoes for r in self.regras.values())
        media_confianca = sum(r.taxa_acerto for r in self.regras.values()) / max(1, total_regras)
        total_ocorrencias_janela = sum(len(r.janela_resultados) for r in self.regras.values())
        
        return {
            'total_regras': total_regras,
            'regras_ativas': regras_ativas,
            'regras_aprendendo': regras_aprendendo,
            'total_inversoes': total_inversoes,
            'media_confianca': round(media_confianca, 1),
            'total_ocorrencias_janela': total_ocorrencias_janela,
            'janela_tamanho': JANELA_TAMANHO,
            'confianca_minima': self.confianca_minima,
            'minimo_amostras': self.minimo_amostras,
            'erros_para_inversao': ERROS_PARA_INVERSAO
        }
    
    def limpar_regras_antigas(self):
        """Remove regras que não tiveram atividade recente"""
        deletadas = self.db.deletar_regras_antigas(DIAS_MANTER_REGRAS)
        
        # Também limpa da memória regras muito antigas
        limite = datetime.now() - timedelta(days=DIAS_MANTER_REGRAS)
        regras_para_remover = []
        for chave, regra in self.regras.items():
            if regra.atualizada_em < limite:
                regras_para_remover.append(chave)
        
        for chave in regras_para_remover:
            del self.regras[chave]
        
        if regras_para_remover:
            logger.info(f"🗑️ {len(regras_para_remover)} regras antigas removidas da memória")
        
        return deletadas


# =============================================================================
# ANALISADOR DE ERROS EM MEMÓRIA
# =============================================================================

class AnalisadorErros:
    def __init__(self):
        self.erros_por_padrao = Counter()
        self.acertos_por_padrao = Counter()
        self.erros_por_confianca = Counter()
        self.acertos_por_confianca = Counter()
        self.ultimos_erros = deque(maxlen=20)
        self.ultimos_acertos = deque(maxlen=20)
        self.erros_por_score = Counter()
        
    def registrar(self, previsao: str, resultado: str, confianca: int, regra: str, 
                  contexto: Dict, player_score: int = None, banker_score: int = None):
        acertou = (previsao == resultado)
        
        faixa_conf = (confianca // 5) * 5
        if acertou:
            self.acertos_por_confianca[faixa_conf] += 1
            self.ultimos_acertos.append({'confianca': confianca, 'regra': regra, 'contexto': contexto})
        else:
            self.erros_por_confianca[faixa_conf] += 1
            self.ultimos_erros.append({'confianca': confianca, 'regra': regra, 'contexto': contexto, 'resultado': resultado})
            if player_score and banker_score:
                self.erros_por_score[f"{player_score}_{banker_score}"] += 1
        
        padrao = self._extrair_padrao(contexto)
        if acertou:
            self.acertos_por_padrao[padrao] += 1
        else:
            self.erros_por_padrao[padrao] += 1
    
    def _extrair_padrao(self, contexto: Dict) -> str:
        streak = contexto.get('streak', 0)
        ultimo_res = contexto.get('ultimo_resultado', 'N/A')
        soma = contexto.get('soma', 0)
        
        if streak >= 3:
            return f"STREAK_{streak}_{ultimo_res}"
        elif 10 <= soma <= 14:
            return f"SOMA_MEDIA_{soma}"
        elif soma >= 20:
            return f"SOMA_ALTA_{soma}"
        elif soma <= 8:
            return f"SOMA_BAIXA_{soma}"
        else:
            return f"PADRAO_NORMAL"
    
    def get_melhores_confiancas(self, min_amostras: int = 5) -> List[int]:
        resultados = []
        for conf in range(65, 100, 5):
            total = self.acertos_por_confianca[conf] + self.erros_por_confianca[conf]
            if total >= min_amostras:
                precisao = (self.acertos_por_confianca[conf] / total) * 100
                if precisao >= 55:
                    resultados.append(conf)
        return resultados
    
    def get_piores_padroes(self, min_amostras: int = 3) -> List[Tuple[str, float]]:
        piores = []
        for padrao in set(self.erros_por_padrao.keys()) | set(self.acertos_por_padrao.keys()):
            total = self.erros_por_padrao[padrao] + self.acertos_por_padrao[padrao]
            if total >= min_amostras:
                precisao = (self.acertos_por_padrao[padrao] / total) * 100
                if precisao < 45:
                    piores.append((padrao, precisao))
        return sorted(piores, key=lambda x: x[1])
    
    def get_analise(self) -> Dict:
        analise = {
            'total_erros': sum(self.erros_por_confianca.values()),
            'total_acertos': sum(self.acertos_por_confianca.values()),
            'melhores_confiancas': self.get_melhores_confiancas(),
            'piores_padroes': self.get_piores_padroes(),
            'ultimos_erros': list(self.ultimos_erros),
            'taxa_erro_recente': 0,
            'acertos_por_confianca': dict(self.acertos_por_confianca),
            'erros_por_confianca': dict(self.erros_por_confianca),
            'erros_por_score': dict(self.erros_por_score.most_common(10))
        }
        
        total_recente = len(self.ultimos_erros) + len(self.ultimos_acertos)
        if total_recente > 0:
            analise['taxa_erro_recente'] = (len(self.ultimos_erros) / total_recente) * 100
        
        return analise


# =============================================================================
# BAC BO BOT ML - VERSÃO 9.1 (JANELA DESLIZANTE + INVERSÃO)
# =============================================================================

class BacBoBotML:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.historico: List[Rodada] = []
        self.analisador_erros = AnalisadorErros()
        
        # GERENCIADOR DE REGRAS COM JANELA DESLIZANTE
        self.gerenciador_regras = GerenciadorRegrasJanela(db)
        
        self.pendente = {
            'ativo': False,
            'previsao': None,
            'regra': None,
            'confianca': 0,
            'confianca_original': 0,
            'rodada_id': None,
            'decisao_id': None,
            'player_score': 0,
            'banker_score': 0,
            'regra_original': None,
            'foi_invertida': False
        }
        
        self.total_apostas = 0
        self.total_acertos = 0
        self.historico_acertos = deque(maxlen=100)
        
        self.limiar_confianca = CONFIANCA_MINIMA
        self.periodo_evolucao = 0
        
        self.emergencia_ativa = False
        self.quantidade_erros_consecutivos = 0
        self.ultimo_acerto = None
        self.sequencia_resultados = deque(maxlen=10)
        
        self._carregar_historico()
    
    def _carregar_historico(self):
        historico = self.db.get_historico(200)
        for rodada in historico:
            self.historico.append(rodada)
            self.sequencia_resultados.append(rodada.resultado)
        logger.info(f"📚 Carregadas {len(self.historico)} rodadas do banco")
    
    def _registrar_evolucao(self):
        periodo = self.total_apostas // 50
        if periodo > self.periodo_evolucao:
            self.periodo_evolucao = periodo
            precisao = (self.total_acertos / max(1, self.total_apostas)) * 100
            self.db.salvar_evolucao(periodo, self.total_apostas, self.total_acertos, precisao, self.limiar_confianca)
            logger.info(f"📈 Evolução registrada: período {periodo}, precisão={precisao:.1f}%")
    
    def _get_contexto(self) -> Dict:
        if len(self.historico) < 2:
            return {}
        
        streak = 0
        ultimo_res = None
        for r in reversed(self.historico):
            if r.resultado == 'TIE':
                continue
            if ultimo_res is None:
                ultimo_res = r.resultado
                streak = 1
            elif r.resultado == ultimo_res:
                streak += 1
            else:
                break
        
        return {
            'streak': streak,
            'ultimo_resultado': ultimo_res,
            'soma': self.historico[-1].soma if self.historico else 0,
            'total_apostas': self.total_apostas
        }
    
    def adicionar_rodada(self, rodada: Rodada):
        self.historico.append(rodada)
        self.sequencia_resultados.append(rodada.resultado)
        if len(self.historico) > HISTORICO_MAX:
            self.historico = self.historico[-HISTORICO_MAX:]
    
    def prever(self, player_score: int, banker_score: int) -> Decisao:
        soma = player_score + banker_score
        
        # NÍVEL 1: Score Exato (PRIORIDADE ABSOLUTA) - JANELA DESLIZANTE
        previsao_score, conf_score, regra_score = self.gerenciador_regras.prever(player_score, banker_score)
        
        if previsao_score and conf_score >= self.limiar_confianca:
            regra_nome = f"SCORE_EXATO_{player_score}_{banker_score}"
            foi_invertida = regra_score.inversoes > 0 if regra_score else False
            
            logger.info(f"🎯 SCORE EXATO (Janela): {player_score} vs {banker_score} → {previsao_score} ({conf_score}%) | Janela: {len(regra_score.janela_resultados) if regra_score else 0}/{JANELA_TAMANHO}")
            if foi_invertida:
                logger.info(f"   🔄 REGRA INVERTIDA {regra_score.inversoes}x")
            
            return Decisao(
                apostar=True,
                previsao=previsao_score,
                regra=regra_nome,
                confianca=conf_score,
                nivel=1,
                player_score=player_score,
                banker_score=banker_score,
                soma=soma,
                timestamp=datetime.now(),
                foi_invertida=foi_invertida,
                regra_original=f"SCORE_EXATO_{player_score}_{banker_score}_original" if foi_invertida else None
            )
        
        # NÍVEL 2: NENHUMA REGRA ATIVA
        logger.debug(f"⏭️ NENHUMA REGRA ATIVA - Não apostar | P={player_score} B={banker_score} Soma={soma}")
        return Decisao(
            apostar=False,
            previsao=None,
            regra='SEM_REGRA_ATIVA',
            confianca=0,
            nivel=10,
            player_score=player_score,
            banker_score=banker_score,
            soma=soma,
            timestamp=datetime.now()
        )
    
    def registrar_aposta(self, rodada_id: str, player_score: int, banker_score: int) -> Tuple[Decisao, Optional[int]]:
        decisao = self.prever(player_score, banker_score)
        decisao_id = None
        
        success, decisao_id = self.db.salvar_decisao(decisao, rodada_id)
        
        if decisao.apostar and success:
            self.pendente = {
                'ativo': True,
                'previsao': decisao.previsao,
                'regra': decisao.regra,
                'confianca': decisao.confianca,
                'confianca_original': decisao.confianca,
                'rodada_id': rodada_id,
                'decisao_id': decisao_id,
                'player_score': player_score,
                'banker_score': banker_score,
                'regra_original': decisao.regra_original,
                'foi_invertida': decisao.foi_invertida
            }
            self.total_apostas += 1
            invertido_msg = " [INVERTIDA]" if decisao.foi_invertida else ""
            logger.info(f"💰 APOSTA REGISTRADA: {decisao.previsao} | {decisao.regra}{invertido_msg} | confiança={decisao.confianca}%")
        else:
            if decisao.regra != 'SEM_REGRA_ATIVA':
                logger.debug(f"⏭️ NENHUMA APOSTA: {decisao.regra}")
        
        return decisao, decisao_id
    
    def validar_aposta(self, resultado: str) -> bool:
        if not self.pendente['ativo']:
            return False
        
        # TIE NÃO É CONSIDERADO ERRO
        if resultado == 'TIE':
            logger.info(f"🟡 TIE (EMPATE) - Não conta como erro | Previsão era: {self.pendente['previsao']}")
            self.db.atualizar_acerto(self.pendente['rodada_id'], True, self.pendente['decisao_id'])
            
            # Registra TIE no gerenciador (não afeta inversão)
            self.gerenciador_regras.registrar_resultado(
                self.pendente['player_score'],
                self.pendente['banker_score'],
                self.pendente['previsao'],
                resultado,
                True,
                self.pendente['regra_original']
            )
            
            self.pendente['ativo'] = False
            return True
        
        acertou = (self.pendente['previsao'] == resultado)
        
        self.ultimo_acerto = acertou
        
        # =========================================================
        # APRENDIZADO EM TEMPO REAL COM JANELA DESLIZANTE E INVERSÃO
        # =========================================================
        regra_atualizada = self.gerenciador_regras.registrar_resultado(
            self.pendente['player_score'],
            self.pendente['banker_score'],
            self.pendente['previsao'],
            resultado,
            acertou,
            self.pendente['regra_original']
        )
        
        if acertou:
            self.quantidade_erros_consecutivos = 0
        else:
            self.quantidade_erros_consecutivos += 1
        
        self.db.atualizar_acerto(self.pendente['rodada_id'], acertou, self.pendente['decisao_id'])
        
        contexto = self._get_contexto()
        padrao = self.analisador_erros._extrair_padrao(contexto)
        
        if acertou:
            self.total_acertos += 1
            self.historico_acertos.append(1)
            self.db.salvar_acerto_para_analise(padrao)
            logger.info(f"✅ ACERTOU! Previsão: {self.pendente['previsao']} = Resultado: {resultado} | Padrão: {padrao}")
            
            if self.emergencia_ativa:
                self.emergencia_ativa = False
                logger.info("✅ Modo emergência desativado após acerto!")
        else:
            self.historico_acertos.append(0)
            
            self.db.salvar_erro(
                rodada_id=self.pendente['rodada_id'],
                decisao_id=self.pendente['decisao_id'],
                previsao=self.pendente['previsao'],
                resultado=resultado,
                confianca=self.pendente['confianca'],
                regra=self.pendente['regra'],
                streak=contexto.get('streak', 0),
                soma_atual=contexto.get('soma', 0),
                ultimo_resultado=contexto.get('ultimo_resultado', ''),
                padrao=padrao,
                foi_invertida=self.pendente['foi_invertida']
            )
            
            logger.info(f"❌ ERROU! Previsão: {self.pendente['previsao']} != Resultado: {resultado} | Padrão: {padrao}")
        
        self.analisador_erros.registrar(
            self.pendente['previsao'],
            resultado,
            self.pendente['confianca'],
            self.pendente['regra'],
            contexto,
            self.pendente['player_score'],
            self.pendente['banker_score']
        )
        
        self._registrar_evolucao()
        
        if self.quantidade_erros_consecutivos >= ERROS_CONSECUTIVOS_LIMITE:
            logger.warning(f"🚨 {self.quantidade_erros_consecutivos} ERROS CONSECUTIVOS! Ativando modo emergência...")
            self.emergencia_ativa = True
        
        # Limpa regras antigas a cada 20 apostas
        if self.total_apostas % 20 == 0 and self.total_apostas > 0:
            self.gerenciador_regras.limpar_regras_antigas()
        
        self.pendente['ativo'] = False
        return acertou
    
    def get_estatisticas_ml(self) -> Dict:
        precisao = (self.total_acertos / max(1, self.total_apostas)) * 100
        analise_erros = self.analisador_erros.get_analise()
        stats_regras = self.gerenciador_regras.get_estatisticas()
        
        ultimas_10 = list(self.historico_acertos)[-10:]
        tendencia_recente = sum(ultimas_10) / max(1, len(ultimas_10)) * 100 if ultimas_10 else 0
        
        return {
            'total_apostas': self.total_apostas,
            'total_acertos': self.total_acertos,
            'precisao': precisao,
            'tendencia_recente': tendencia_recente,
            'limiar_confianca': self.limiar_confianca,
            'emergencia_ativa': self.emergencia_ativa,
            'erros_consecutivos': self.quantidade_erros_consecutivos,
            'analise_erros': analise_erros,
            'stats_regras': stats_regras,
            'melhores_confiancas': analise_erros.get('melhores_confiancas', []),
            'taxa_erro_recente': analise_erros.get('taxa_erro_recente', 0)
        }


# =============================================================================
# FLASK APP
# =============================================================================

app = Flask(__name__)
CORS(app)

db = DatabaseManager(DATABASE_URL)
bot = BacBoBotML(db)

cache = {
    'ultima_rodada': None,
    'ultima_previsao': None,
    'fonte_ativa': 'api_direto',
    'rodadas_total': 0
}


# =============================================================================
# ALTERAÇÃO PRINCIPAL v9.2 - FUNÇÃO DE BUSCA COM FALLBACK
# =============================================================================

def buscar_rodada_real() -> Optional[Rodada]:
    """
    Busca rodada da API seguindo esta ordem:
    1. Tenta API_DIRETO (principal)
    2. Se falhar, tenta LATEST_API_URL (fallback)
    3. Se ambos falharem, retorna None
    """
    
    # ===== TENTA API DIRETO PRIMEIRO =====
    try:
        response = requests.get(API_DIRETO, headers=HEADERS, timeout=5)
        
        if response.status_code == 200:
            dados = response.json()
            
            # A API Direto retorna uma lista/array
            if isinstance(dados, list) and len(dados) > 0:
                # Pega a primeira rodada da lista (mais recente)
                item = dados[0]
                rodada_id = item.get('id') or item.get('_id')
                
                # Verifica se já processou esta rodada
                if rodada_id in ULTIMO_ID_CONTROLE:
                    cache['fonte_ativa'] = 'api_direto'
                    return None
                
                ULTIMO_ID_CONTROLE[rodada_id] = True
                
                # Extrai os dados da rodada
                if 'data' in item and 'result' in item.get('data', {}):
                    data_obj = item['data']
                    result = data_obj.get('result', {})
                    player_dice = result.get('playerDice', {})
                    banker_dice = result.get('bankerDice', {})
                    player_score = player_dice.get('first', 0) + player_dice.get('second', 0)
                    banker_score = banker_dice.get('first', 0) + banker_dice.get('second', 0)
                    outcome = result.get('outcome', '')
                    
                    if outcome == 'PlayerWon':
                        resultado = 'PLAYER'
                    elif outcome == 'BankerWon':
                        resultado = 'BANKER'
                    else:
                        resultado = 'TIE'
                else:
                    player_score = item.get('player_score', 0)
                    banker_score = item.get('banker_score', 0)
                    resultado = item.get('resultado', '')
                
                if player_score == 0 and banker_score == 0:
                    cache['fonte_ativa'] = 'api_direto'
                    return None
                
                cache['fonte_ativa'] = 'api_direto'
                logger.debug(f"📡 Fonte: API DIRETO - ID: {rodada_id}")
                
                return Rodada(
                    id=rodada_id,
                    data_hora=datetime.now(),
                    player_score=player_score,
                    banker_score=banker_score,
                    resultado=resultado,
                    fonte='api_direto'
                )
            else:
                logger.warning(f"⚠️ API DIRETO retornou formato inesperado: {type(dados)}")
        else:
            logger.warning(f"⚠️ API DIRETO falhou com status {response.status_code}")
            
    except Exception as e:
        logger.warning(f"⚠️ Erro na API DIRETO: {e}")
    
    # ===== FALLBACK: TENTA API LATEST =====
    logger.info("🔄 Tentando fallback com API LATEST...")
    
    try:
        response = requests.get(LATEST_API_URL, headers=HEADERS, timeout=5)
        
        if response.status_code != 200:
            logger.error(f"❌ API LATEST também falhou! Status: {response.status_code}")
            return None
        
        dados = response.json()
        rodada_id = dados.get('id') or dados.get('_id')
        
        if rodada_id in ULTIMO_ID_CONTROLE:
            cache['fonte_ativa'] = 'api_latest'
            return None
        
        ULTIMO_ID_CONTROLE[rodada_id] = True
        
        if 'data' in dados and 'result' in dados['data']:
            data_obj = dados['data']
            result = data_obj.get('result', {})
            player_dice = result.get('playerDice', {})
            banker_dice = result.get('bankerDice', {})
            player_score = player_dice.get('first', 0) + player_dice.get('second', 0)
            banker_score = banker_dice.get('first', 0) + banker_dice.get('second', 0)
            outcome = result.get('outcome', '')
            
            if outcome == 'PlayerWon':
                resultado = 'PLAYER'
            elif outcome == 'BankerWon':
                resultado = 'BANKER'
            else:
                resultado = 'TIE'
        else:
            player_score = dados.get('player_score', 0)
            banker_score = dados.get('banker_score', 0)
            resultado = dados.get('resultado', '')
        
        if player_score == 0 and banker_score == 0:
            cache['fonte_ativa'] = 'api_latest'
            return None
        
        cache['fonte_ativa'] = 'api_latest'
        logger.info(f"📡 Fonte: API LATEST (fallback) - ID: {rodada_id}")
        
        return Rodada(
            id=rodada_id,
            data_hora=datetime.now(),
            player_score=player_score,
            banker_score=banker_score,
            resultado=resultado,
            fonte='api_latest'
        )
        
    except Exception as e:
        logger.error(f"❌ Erro no fallback da API LATEST: {e}")
        return None


def processar_rodada(rodada: Rodada):
    global cache
    
    if cache['ultima_rodada']:
        bot.validar_aposta(rodada.resultado)
    
    cache['ultima_rodada'] = rodada
    db.salvar_rodada(rodada)
    bot.adicionar_rodada(rodada)
    cache['rodadas_total'] = db.get_total_rodadas()
    
    logger.info(f"🎲 RODADA #{cache['rodadas_total']}: P={rodada.player_score} B={rodada.banker_score} | {rodada.resultado} | Fonte: {rodada.fonte}")
    
    decisao, decisao_id = bot.registrar_aposta(rodada.id, rodada.player_score, rodada.banker_score)
    
    if decisao.apostar:
        cache['ultima_previsao'] = {
            'previsao': decisao.previsao,
            'regra': decisao.regra,
            'confianca': decisao.confianca,
            'nivel': decisao.nivel,
            'base': f"P={decisao.player_score} B={decisao.banker_score}",
            'soma': decisao.soma,
            'foi_invertida': decisao.foi_invertida
        }
    else:
        cache['ultima_previsao'] = None
    
    total_apostas, total_acertos, precisao = db.get_estatisticas()
    if total_apostas > 0:
        logger.info(f"📊 BOT: {total_apostas} apostas | {total_acertos} acertos | {precisao:.1f}%")
    
    stats_ml = bot.get_estatisticas_ml()
    if stats_ml['total_apostas'] > 0:
        logger.info(f"🤖 ML: {stats_ml['total_apostas']} apostas | {stats_ml['precisao']:.1f}%")
        logger.info(f"📈 Taxa erro recente: {stats_ml['taxa_erro_recente']:.1f}%")
        if stats_ml.get('stats_regras', {}).get('total_inversoes', 0) > 0:
            logger.info(f"🔄 Regras invertidas: {stats_ml['stats_regras']['total_inversoes']}")
        if stats_ml.get('stats_regras', {}).get('regras_aprendendo', 0) > 0:
            logger.info(f"📚 Regras em aprendizado: {stats_ml['stats_regras']['regras_aprendendo']}")
    
    logger.info("-" * 70)


def loop_coleta():
    logger.info("🔄 Loop de coleta iniciado (2s)")
    
    while True:
        try:
            rodada = buscar_rodada_real()
            
            if rodada:
                processar_rodada(rodada)
            
            time.sleep(2)
            
        except Exception as e:
            logger.error(f"Erro no loop: {e}")
            time.sleep(5)


# =============================================================================
# ROTAS PRINCIPAIS
# =============================================================================

@app.route('/')
def index():
    try:
        return render_template('index.html')
    except Exception as e:
        logger.error(f"Erro ao carregar template: {e}")
        return f"Erro ao carregar dashboard: {e}", 500


@app.route('/api/stats')
def api_stats():
    try:
        total_apostas, total_acertos, precisao = db.get_estatisticas()
        total_rodadas = db.get_total_rodadas()
        stats_ml = bot.get_estatisticas_ml()
        
        return jsonify({
            'total_apostas': total_apostas,
            'total_acertos': total_acertos,
            'precisao': precisao,
            'total_rodadas': total_rodadas,
            'fonte_ativa': cache.get('fonte_ativa', 'api_direto'),
            'ml_stats': stats_ml,
            'ultima_previsao': cache.get('ultima_previsao'),
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Erro em api_stats: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/rodadas')
def api_rodadas():
    try:
        limit = request.args.get('limit', 50, type=int)
        rodadas = db.get_rodadas_limit(limit)
        return jsonify(rodadas)
    except Exception as e:
        logger.error(f"Erro em api_rodadas: {e}")
        return jsonify([])


@app.route('/api/historico')
def api_historico():
    try:
        historico = db.get_historico_apostas(30)
        return jsonify(historico)
    except Exception as e:
        logger.error(f"Erro em api_historico: {e}")
        return jsonify([])


@app.route('/api/regras')
def api_regras():
    try:
        regras = bot.gerenciador_regras.get_regras_para_frontend(50)
        invertidas = bot.gerenciador_regras.get_regras_invertidas()
        stats = bot.gerenciador_regras.get_estatisticas()
        
        return jsonify({
            'success': True,
            'total_regras': len(regras),
            'regras': regras,
            'regras_invertidas': invertidas,
            'estatisticas': stats,
            'configuracoes': {
                'janela_tamanho': JANELA_TAMANHO,
                'confianca_minima': CONFIANCA_MINIMA,
                'amostras_minimas': AMOSTRAS_MINIMAS,
                'erros_para_inversao': ERROS_PARA_INVERSAO,
                'dias_manter_regras': DIAS_MANTER_REGRAS
            },
            'fonte_ativa': cache.get('fonte_ativa', 'api_direto'),
            'ultima_atualizacao': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Erro em api_regras: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/evolucao')
def api_evolucao():
    try:
        evolucao = db.get_evolucao(50)
        return jsonify({
            'precisao': [{'periodo': e['periodo'], 'precisao': e['precisao']} for e in evolucao],
            'limiar': [{'periodo': e['periodo'], 'limiar': e['limiar']} for e in evolucao]
        })
    except Exception as e:
        logger.error(f"Erro em api_evolucao: {e}")
        return jsonify({'precisao': [], 'limiar': []})


@app.route('/api/performance/confianca')
def api_performance_confianca():
    try:
        analise = bot.analisador_erros.get_analise()
        
        faixas = {}
        for conf in range(65, 100, 5):
            total = analise.get('acertos_por_confianca', {}).get(conf, 0) + analise.get('erros_por_confianca', {}).get(conf, 0)
            if total > 0:
                acertos = analise.get('acertos_por_confianca', {}).get(conf, 0)
                faixas[conf] = {
                    'total': total,
                    'acertos': acertos,
                    'precisao': (acertos / total) * 100
                }
        
        recomendacao = "Ajuste o limiar de confiança para melhor desempenho"
        if analise.get('melhores_confiancas'):
            recomendacao = f"Melhores faixas de confiança: {analise['melhores_confiancas']}"
        
        return jsonify({
            'faixas_confianca': faixas,
            'recomendacao': recomendacao
        })
    except Exception as e:
        logger.error(f"Erro em api_performance_confianca: {e}")
        return jsonify({})


@app.route('/api/simular')
def api_simular():
    try:
        limite = request.args.get('limite', 1000, type=int)
        aposta = request.args.get('aposta', 5, type=float)
        
        historico = db.get_historico(limite)
        
        if len(historico) < 10:
            return jsonify({'erro': 'Histórico insuficiente para simulação'}), 400
        
        saldo = 0
        apostas_realizadas = 0
        acertos = 0
        
        for i in range(len(historico) - 1):
            ultima = historico[i]
            if ultima.resultado == 'PLAYER':
                previsao = 'BANKER'
                confianca = 65
            else:
                previsao = 'PLAYER'
                confianca = 65
            
            if confianca >= 60:
                apostas_realizadas += 1
                if previsao == historico[i + 1].resultado:
                    acertos += 1
                    saldo += aposta
                else:
                    saldo -= aposta
        
        precisao = (acertos / max(1, apostas_realizadas)) * 100
        lucro_percentual = (saldo / (apostas_realizadas * aposta)) * 100 if apostas_realizadas > 0 else 0
        
        return jsonify({
            'total_rodadas': len(historico),
            'apostas_realizadas': apostas_realizadas,
            'acertos': acertos,
            'precisao': precisao,
            'saldo_final': saldo,
            'lucro_percentual': lucro_percentual
        })
    except Exception as e:
        logger.error(f"Erro em api_simular: {e}")
        return jsonify({'erro': str(e)}), 500


@app.route('/api/simular/cenarios')
def api_simular_cenarios():
    try:
        limite = 500
        historico = db.get_historico(limite)
        
        if len(historico) < 10:
            return jsonify({'erro': 'Histórico insuficiente'}), 400
        
        cenarios = []
        for aposta_valor in [1, 5, 10, 25, 50, 100]:
            saldo = 0
            apostas_realizadas = 0
            acertos = 0
            
            for i in range(len(historico) - 1):
                ultima = historico[i]
                if ultima.resultado == 'PLAYER':
                    previsao = 'BANKER'
                    confianca = 65
                else:
                    previsao = 'PLAYER'
                    confianca = 65
                
                if confianca >= 60:
                    apostas_realizadas += 1
                    if previsao == historico[i + 1].resultado:
                        acertos += 1
                        saldo += aposta_valor
                    else:
                        saldo -= aposta_valor
            
            precisao = (acertos / max(1, apostas_realizadas)) * 100
            lucro_percentual = (saldo / (apostas_realizadas * aposta_valor)) * 100 if apostas_realizadas > 0 else 0
            
            cenarios.append({
                'aposta': aposta_valor,
                'apostas_realizadas': apostas_realizadas,
                'acertos': acertos,
                'precisao': precisao,
                'saldo_final': saldo,
                'lucro_percentual': lucro_percentual
            })
        
        return jsonify({'cenarios': cenarios})
    except Exception as e:
        logger.error(f"Erro em api_simular_cenarios: {e}")
        return jsonify({'erro': str(e)}), 500


@app.route('/api/erros/estatisticas')
def api_erros_estatisticas():
    try:
        stats = db.get_estatisticas_erros()
        return jsonify({
            'success': True,
            'data': stats,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Erro em api_erros_estatisticas: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/erros/ultimos')
def api_erros_ultimos():
    try:
        limit = request.args.get('limit', 20, type=int)
        offset = request.args.get('offset', 0, type=int)
        limit = min(limit, 100)
        
        erros = db.get_ultimos_erros(limit, offset)
        total_erros = db.get_total_erros()
        
        return jsonify({
            'success': True,
            'data': {
                'erros': erros,
                'total': total_erros,
                'limit': limit,
                'offset': offset,
                'has_more': (offset + limit) < total_erros
            },
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Erro em api_erros_ultimos: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/erros/piores_padroes')
def api_erros_piores_padroes():
    try:
        limit = request.args.get('limit', 10, type=int)
        min_ocorrencias = request.args.get('min_ocorrencias', 3, type=int)
        limit = min(limit, 50)
        
        piores = db.get_piores_padroes(limit, min_ocorrencias)
        
        return jsonify({
            'success': True,
            'data': {
                'padroes': piores,
                'total_encontrados': len(piores),
                'min_ocorrencias': min_ocorrencias,
                'limit': limit
            },
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Erro em api_erros_piores_padroes: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/erros/melhores_padroes')
def api_erros_melhores_padroes():
    try:
        limit = request.args.get('limit', 10, type=int)
        min_ocorrencias = request.args.get('min_ocorrencias', 3, type=int)
        limit = min(limit, 50)
        
        melhores = db.get_melhores_padroes(limit, min_ocorrencias)
        
        return jsonify({
            'success': True,
            'data': {
                'padroes': melhores,
                'total_encontrados': len(melhores),
                'min_ocorrencias': min_ocorrencias,
                'limit': limit
            },
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Erro em api_erros_melhores_padroes: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/erros/por_confianca')
def api_erros_por_confianca():
    try:
        min_amostras = request.args.get('min_amostras', 3, type=int)
        stats = db.get_erros_por_confianca(min_amostras)
        
        return jsonify({
            'success': True,
            'data': {
                'faixas': stats,
                'min_amostras': min_amostras
            },
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Erro em api_erros_por_confianca: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/erros/por_regra')
def api_erros_por_regra():
    try:
        min_amostras = request.args.get('min_amostras', 3, type=int)
        stats = db.get_erros_por_regra(min_amostras)
        
        return jsonify({
            'success': True,
            'data': {
                'regras': stats,
                'min_amostras': min_amostras
            },
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Erro em api_erros_por_regra: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/erros/por_streak')
def api_erros_por_streak():
    try:
        stats = db.get_erros_por_streak()
        
        return jsonify({
            'success': True,
            'data': stats,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Erro em api_erros_por_streak: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/erros/por_soma')
def api_erros_por_soma():
    try:
        min_amostras = request.args.get('min_amostras', 3, type=int)
        stats = db.get_erros_por_soma(min_amostras)
        
        return jsonify({
            'success': True,
            'data': {
                'faixas_soma': stats,
                'min_amostras': min_amostras
            },
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Erro em api_erros_por_soma: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/erros/detalhe/<int:erro_id>')
def api_erros_detalhe(erro_id):
    try:
        erro = db.get_erro_detalhado(erro_id)
        
        if not erro:
            return jsonify({
                'success': False,
                'error': f'Erro com ID {erro_id} não encontrado'
            }), 404
        
        return jsonify({
            'success': True,
            'data': erro,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Erro em api_erros_detalhe: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/erros/resumo')
def api_erros_resumo():
    try:
        estatisticas_gerais = db.get_estatisticas_erros()
        piores_padroes = db.get_piores_padroes(5, 2)
        melhores_padroes = db.get_melhores_padroes(5, 2)
        ultimos_erros = db.get_ultimos_erros(10, 0)
        erros_por_confianca = db.get_erros_por_confianca(3)
        
        total_apostas, total_acertos, precisao = db.get_estatisticas()
        
        return jsonify({
            'success': True,
            'data': {
                'visao_geral': {
                    'total_apostas': total_apostas,
                    'total_acertos': total_acertos,
                    'precisao_global': precisao,
                    'total_erros': estatisticas_gerais.get('total_erros', 0),
                    'taxa_erro_global': (estatisticas_gerais.get('total_erros', 0) / max(1, total_apostas)) * 100 if total_apostas > 0 else 0
                },
                'piores_padroes': piores_padroes,
                'melhores_padroes': melhores_padroes,
                'ultimos_erros': ultimos_erros,
                'erros_por_confianca': erros_por_confianca,
                'erros_por_regra': estatisticas_gerais.get('erros_por_regra', []),
                'recomendacoes': gerar_recomendacoes(piores_padroes, erros_por_confianca)
            },
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Erro em api_erros_resumo: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def gerar_recomendacoes(piores_padroes, erros_por_confianca):
    recomendacoes = []
    
    if piores_padroes and len(piores_padroes) > 0:
        pior = piores_padroes[0]
        if pior.get('taxa_erro', 0) > 70:
            recomendacoes.append({
                'tipo': 'EVITAR_PADRAO',
                'mensagem': f"Evite apostar quando identificar o padrão '{pior.get('padrao')}' (taxa de erro de {pior.get('taxa_erro', 0):.1f}%)",
                'severidade': 'alta'
            })
    
    if erros_por_confianca:
        faixas_ruins = [f for f in erros_por_confianca if f.get('taxa_erro', 0) > 60]
        if faixas_ruins:
            recomendacoes.append({
                'tipo': 'AJUSTAR_CONFIANCA',
                'mensagem': f"Evite apostas com confiança {faixas_ruins[0].get('faixa')} (taxa de erro de {faixas_ruins[0].get('taxa_erro', 0):.1f}%)",
                'severidade': 'media'
            })
    
    if not recomendacoes:
        recomendacoes.append({
            'tipo': 'INFO',
            'mensagem': "Continue monitorando, ainda não há padrões claros de erro",
            'severidade': 'baixa'
        })
    
    return recomendacoes


@app.route('/api/debug/check')
def debug_check():
    try:
        conn = db._get_connection()
        if not conn:
            return jsonify({'erro': 'Sem conexão com banco'})
        
        cur = conn.cursor()
        
        cur.execute("SELECT COUNT(*) FROM rodadas")
        total_rodadas = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM decisoes WHERE apostar = TRUE")
        total_apostas = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM erros")
        total_erros = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM analise_padroes")
        total_padroes = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM regras_aprendidas")
        total_regras = cur.fetchone()[0]
        
        cur.execute("SELECT id, previsao, resultado, created_at FROM erros ORDER BY id DESC LIMIT 5")
        ultimos_erros = cur.fetchall()
        
        cur.close()
        conn.close()
        
        return jsonify({
            'total_rodadas': total_rodadas,
            'total_apostas': total_apostas,
            'total_erros': total_erros,
            'total_padroes': total_padroes,
            'total_regras': total_regras,
            'ultimos_erros': [{'id': e[0], 'previsao': e[1], 'resultado': e[2], 'data': str(e[3])} for e in ultimos_erros],
            'mensagem': 'Verifique se há apostas e erros registrados'
        })
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("\n" + "="*70)
    print("🚀 BAC BO BOT - ML EVOLUTION v9.2")
    print("🎯 NOVIDADES v9.2:")
    print("   ✅ API DIRETO como fonte PRINCIPAL (page=0&size=10&sort=data.settledAt,desc)")
    print("   ✅ API LATEST como FALLBACK (quando o direto trava)")
    print("   ✅ Comutação automática entre fontes")
    print("   ✅ JANELA DESLIZANTE (Sliding Window) - últimas 20 ocorrências por score")
    print("   ✅ APRENDE DO ZERO - sem tabela estática, apenas dados reais")
    print("   ✅ INVERSÃO AUTOMÁTICA - 2 erros consecutivos = regra invertida")
    print("   ✅ DESCARTE AUTOMÁTICO - regras antigas são removidas após 7 dias")
    print("   ✅ TIE não é considerado erro (não afeta aprendizado)")
    print("   ✅ Score Exato tem PRIORIDADE ABSOLUTA")
    print("   ✅ Aprendizado contínuo em tempo real")
    print(f"\n📊 Configurações:")
    print(f"   Janela deslizante: {JANELA_TAMANHO} ocorrências por score")
    print(f"   Confiança mínima: {CONFIANCA_MINIMA}%")
    print(f"   Amostras mínimas: {AMOSTRAS_MINIMAS}")
    print(f"   Erros para inversão: {ERROS_PARA_INVERSAO}")
    print(f"   Dias para descartar regras: {DIAS_MANTER_REGRAS}")
    print(f"   Erros consecutivos limite: {ERROS_CONSECUTIVOS_LIMITE}")
    print(f"\n📚 Modo de aprendizado: DO ZERO (sem tabela estática)")
    print(f"\n📡 Fontes API:")
    print(f"   → PRINCIPAL: {API_DIRETO}")
    print(f"   → FALLBACK:  {LATEST_API_URL}")
    print("⏱️  Polling: 2 segundos")
    print("="*70)
    
    coleta_thread = threading.Thread(target=loop_coleta, daemon=True)
    coleta_thread.start()
    
    logger.info(f"🌐 Servidor na porta {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
