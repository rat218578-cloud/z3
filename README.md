# BAC BO BOT - TESE v3.0

Bot para previsão de resultados do jogo Bac Bo baseado em análise de 2.254 rodadas reais.

## 📊 Estratégia

O bot utiliza a **TESE v2.0** que identificou dois padrões estatisticamente verificáveis:

- **Camada A**: Memória de Scores (regras de score exato)
- **Camada B**: Contexto de Soma (soma total dos dados)

## 🎯 Precisão

| Sistema | Precisão | Apostas |
|---------|----------|---------|
| Scores + Soma | 64.8% | 12% das rodadas |
| Regras Fortes | 75.8% | 5% das rodadas |

## 🚀 Como usar

1. Clone o repositório
2. Instale as dependências: `pip install -r requirements.txt`
3. Execute: `python main.py`
4. Acesse: `http://localhost:5000`

## 📁 Estrutura
