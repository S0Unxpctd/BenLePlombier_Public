# BenLePlombier/Souffl.ai — Devis vocaux via WhatsApp

> Un plombier dicte un chantier sur WhatsApp. Trente secondes plus tard, il reçoit un devis professionnel en PDF, prêt à envoyer au client.

SaaS B2B en production, conçu pour les artisans qui détestent l'admin. Mémo vocal → transcription → génération IA structurée → PDF → renvoi WhatsApp. Pas d'app à installer, pas de compte à créer, pas d'interface à apprendre.

## Pourquoi ce projet existe

Un artisan plombier passe en moyenne 5 à 10 heures par semaine à faire des devis. Le soir, le week-end, ou pas du tout — auquel cas il perd le chantier. L'idée : retirer 100% du clavier de l'équation. Tout se passe sur l'app qu'ils utilisent déjà 50 fois par jour : WhatsApp.

## Comment ça marche

```
┌──────────────┐    ┌──────────┐    ┌──────────┐    ┌────────────┐    ┌──────────────┐
│ Mémo vocal   │ →  │ Whisper  │ →  │  Claude  │ →  │ WeasyPrint │ →  │  PDF sur     │
│ WhatsApp     │    │ (STT)    │    │  (JSON)  │    │  (HTML→PDF)│    │  WhatsApp    │
└──────────────┘    └──────────┘    └──────────┘    └────────────┘    └──────────────┘
```

1. **Réception** — WhatsApp Business Cloud API → webhook FastAPI
2. **Transcription** — Whisper transforme l'audio en texte (FR)
3. **Génération** — Claude (via OpenRouter) structure le devis en JSON, contraint par un system prompt avec grille tarifaire embarquée + caps de lignes + détection d'incohérences
4. **Récap interactif** — l'artisan voit le récap en boutons WhatsApp : valider, modifier (texte ou nouveau vocal), supprimer une ligne
5. **PDF** — Jinja2 + WeasyPrint produisent le PDF avec en-tête entreprise, mentions légales, TVA, acompte
6. **Persistance** — Postgres stocke le profil artisan, le devis JSON, les factures dérivées (acompte / intermédiaire / solde)

## Stack

| Layer | Tech |
|---|---|
| Interface | WhatsApp Business Cloud API (Meta) |
| Backend | Python 3.13 · FastAPI · Uvicorn |
| IA | Whisper (transcription) · Claude via OpenRouter (génération) |
| PDF | WeasyPrint + Jinja2 |
| DB | PostgreSQL (Railway) |
| Hosting | Railway |
| Mail | Resend API |
| Observabilité | Sentry + backups S3-compat |

## Architecture

Architecture **transport-agnostique** : la logique métier ne sait pas qu'elle parle à WhatsApp.

```
core/                    # Logique métier pure, testable sans réseau
  quote_flow.py            ── machine à états du flow devis
  edit_flow.py             ── modification ligne par ligne (texte + vocal)
  profile_flow.py          ── onboarding artisan (12 étapes)
  state_store.py           ── persistance des sessions conversationnelles

whatsapp/                # Adaptateur WhatsApp uniquement
  webhook.py               ── endpoint FastAPI + vérif HMAC Meta
  router.py                ── parse inbound → dispatch
  flows.py                 ── colle entre WhatsApp et core/
  send.py                  ── client API sortant (text, buttons, media)
  media.py                 ── download/upload média (audio, PDF)
  debounce.py              ── regroupement multi-vocaux
  commands.py              ── commandes admin
  users.py                 ── mapping E.164 ↔ user_id

ops/                     # Sentry, backups Postgres
tests/                   # 22 fichiers, ~9k lignes, mocks réseau
```

**Principe de design** : le jour où on ajoute une UI web ou un canal SMS, seul un nouvel adaptateur change — `core/` reste intact.

## Ce qui est en production

- Pipeline complète vocal → PDF (`< 30 s` p50)
- Onboarding conversationnel 12 étapes (raison sociale, SIRET, RIB, tarifs, logo, mentions légales…)
- Modification de devis par message texte **ou** nouveau vocal ("rajoute un mitigeur à 80 euros")
- Factures d'acompte, intermédiaire, solde générées depuis un devis
- Envoi du PDF par email au client (via Resend)
- Debounce multi-vocaux (artisan envoie 3 vocaux en rafale → un seul devis)
- Déduplication webhook (mémoire + Postgres, idempotent)
- Vérification HMAC `X-Hub-Signature-256` sur tous les inbound Meta

## Methodology

Ce repo a été développé avec un workflow **multi-agent IA** : un agent Builder écrit le code, un agent Reviewer le lit indépendamment contre le spec, un agent QA exécute la suite de tests avant tout merge. Strict QA gate : 100% des tests déclarés doivent passer.

Les docs `BACKLOG.md`, `DECISIONS.md`, `PROGRESS.md`, `STATE.md`, `CHECKPOINT.md` documentent en clair les arbitrages techniques et l'état du projet à chaque session. C'est volontairement public — la transparence du raisonnement vaut autant que le code lui-même.

## Roadmap

Phases livrées :
- **0** — ops (Sentry, backups, idempotence)
- **1** — refactor `core/` transport-agnostique
- **2** — adaptateur WhatsApp MVP
- **2.5** — system prompt v2 (grille tarifaire embarquée, caps, cohérence)
- **3** — UX upgrades (boutons interactifs, modif vocale)

Phases à venir :
- **4** — onboarding via WhatsApp Flows natifs
- **5** — Vision (photo de chantier → lignes de devis)
- **6** — templates Meta + rétention
- **7** — frontend web + liens de partage de devis

## Setup local

```bash
git clone https://github.com/S0Unxpctd/BenLePlombierWhatsApp.git
cd BenLePlombierWhatsApp

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Remplir les variables (cf. .env.example)

python db_init.py          # première fois uniquement
uvicorn whatsapp.webhook:_lazy_app --reload
```

Pour les tests :

```bash
pytest tests/ -v
```

## Déploiement Railway

1. New Project → Deploy from GitHub → ce repo
2. Add service → PostgreSQL
3. Variables d'environnement (cf. `.env.example`)
4. Webhook Meta pointé sur `https://<service>.railway.app/webhook`
5. Vérification du webhook avec `WHATSAPP_VERIFY_TOKEN`

## Licence

Code source publié à des fins de portfolio. Le produit Souffl.AI lui-même reste propriétaire.

---

*Construit en bossant avec Claude — méthodologie multi-agent, sessions tracées, decisions documentées. [So](https://github.com/S0Unxpctd) · 2026*
