# Stand von agent.dfx.swiss gegenüber KONZEPT.md

- **Stand:** 2026-11-26, Integrationsstand `develop` Commit `232b3fa7ce`; Branches und offene PRs vom selben Abend.
- **Nachtrag:** Nach diesem Stand kam `717982cf` auf `develop` (Branch `ad5f2026-kor-73`: Abos nebeneinander
  anmelden, je mit eigenem Postfach und eigener Browser-Sandbox, Prüfung für Menschen durch den Inhaber,
  Registrierung neuer Konten). Damit ist E-33 auf `develop` gebaut (nicht live) und E-39 für die Abo-Anmeldung
  begonnen; Zählung und Tabellen unten bleiben beim Stand `232b3fa7ce`.
- **Produktion** (2026-11-26, 17 Uhr, Angabe des Auftraggebers): Hub und Weboberfläche auf `develop`-Commit
  `9273856bb`, Runner und Gast auf Release 34 (`682fc189`). Was danach auf `develop` kam, ist noch nicht live
  ([Abschnitt 5](#5-produktionsstand)).
- **Gegenstand:** jedes Element von [KONZEPT.md](../KONZEPT.md) – Teil A (Grundprinzip, §1–§14, Ideen 26–47, §16) und
  Teil B (E-01 bis E-36 auf `develop`) – sowie die Regeln, die erst auf Branches stehen (`ad5f2026-doc-10`: ADR-098
  §7; `ad5f2026-doc-11`: E-37 bis E-42).
- **Quellen, wiederverwendet:** die Zeilen „Umsetzung:“ in KONZEPT.md, die Rückverfolgung
  [spec/01-requirements.md](../spec/01-requirements.md) (R-Blöcke mit Spec und Tests), die Gesamtabnahme vom
  23.11. ([acceptance/FINAL.md](../acceptance/FINAL.md), [decisions/ACC-FINAL.md](../decisions/ACC-FINAL.md)), die
  ADRs ([adr/README.md](../adr/README.md)), der Umzugsplan zu E-32 ([decisions/KOR-72.md](../decisions/KOR-72.md),
  [ADR-098](../adr/098-working-modes.md)) und das CI-Konzept ([spec/16-ci-runners.md](../spec/16-ci-runners.md)).
  Jeder Befund ist zusätzlich am Code oder an den Tests geprüft (Pfade als `code`). Wo eine Umsetzungszeile vom
  gebauten Stand abweicht, steht das in [Abschnitt 6](#6-abweichungen-der-dokumentation-vom-gebauten-stand).

Dieses Dokument ändert keine Regel und keine Umsetzungszeile; es beschreibt nur den Stand.

**Stand-Werte.** **erledigt** – im Code auf `develop` mit Tests (bei §13 und den Vorschlägen: entschieden und so
gebaut) · **teilweise** – gebaut bis auf einen benannten Rest · **in Arbeit** – offener PR oder Branch im Repository
bzw. laufende Aufgabe · **offen** – beauftragt bzw. Regel des Auftraggebers, nichts gebaut und nichts in Arbeit ·
**nur Konzept** – ausdrücklich nicht beauftragt · **blockiert** – der Rest wartet auf einen Entscheid des
Auftraggebers.

**Größe des Rests.** S – ein kleines Paket; M – ein bis zwei Pakete mit eigener ADR; L – mehrere Pakete in Folge.
Keine Termine.

## 1. Zusammenfassung

| Teil | Elemente | erledigt | teilweise | in Arbeit | offen | nur Konzept | blockiert |
|---|---|---|---|---|---|---|---|
| A – Konzept (Grundprinzip, §1–§14, Ideen 26–47, §16; je Punkt) | 125 | 119 | 6 | 0 | 0 | 0 | 0 |
| B – Regeln auf `develop` (E-01 – E-36) | 36 | 30 | 0 | 2 | 0 | 0 | 4 |
| B – Regeln und Entscheide auf Branches (E-37 – E-42, ADR-098 §7) | 7 | 1 | 0 | 5 | 0 | 1 | 0 |
| **Teil B gesamt** | **43** | **31** | **0** | **7** | **0** | **1** | **4** |

Kurz:

- **Teil A ist bis auf sechs Reste gebaut.** Die Gesamtabnahme vom 23.11. hat 153 von 153 Anforderungen der Blöcke
  R-0 bis R-15 und R-P belegt; die Ideen 27–47 (Block R-16) sind seither in den KOR-Paketen gebaut. Die Reste:
  Live-Nachweis der Mail-Automatik ([§4 P3](../KONZEPT.md#k-4-b3)), Übergabe des Monitoring-Patches
  ([§7 P4](../KONZEPT.md#k-7-b4)), die nicht beauftragte Vertrauensverankerung des Runners KOR-55
  ([§5 P3](../KONZEPT.md#k-5-b3), [§8 P3](../KONZEPT.md#k-8-b3), [§16 P7](../KONZEPT.md#k-16-b7)) und parallele
  Plattform-Vorgänge ([§16 P1](../KONZEPT.md#k-16-b1), über E-33, in Arbeit).
- **Teil B:** E-32 (Arbeitsweisen) und E-33 (parallele Vorgänge) sind in Arbeit (Branches `ad5f2026-kor-75`,
  `ad5f2026-kor-73`). E-34 bis E-36 (CI-Läufe über die Plattform) sind konzipiert, aber ohne Code; sie warten auf die
  Entscheide von spec/16 §13. Von E-11 ist alles gebaut; die Schalter legt der Betreiber erst nach dem Entscheid des
  Auftraggebers um.
- **Auf Branches:** E-37 (Postfach als Plattform-Werkzeug) ist auf `develop` schon gebaut (KOR-74, ADR-099, E2E-74),
  die Regel selbst steht erst auf `ad5f2026-doc-11`. In Arbeit sind E-39 (Fernsteuerung), E-40 (angemeldet bleiben,
  PR 266), E-41 (Tasks wie Tickets), E-42 (Landing-Page) und ADR-098 §7. E-38 (Agent-Tresor) ist ausdrücklich nur
  Konzept.
- **Die Dokumentation hinkt dem Code hinterher:** Teil D von KONZEPT.md, Block R-16 und Block R-E führen elf gebaute
  Punkte noch als „offen“ oder „teils“. Auf den Branches kollidieren Nummern: E-37, ADR-099 und KOR-74 je zweimal
  (Abschnitt 6).

## 2. Je Element

Spalten: Element (Link in KONZEPT.md) · Stand · Belege (Anforderung, ADR, Code, Tests) · was fehlt · Paket oder PR ·
Rest · Abhängigkeiten. „R-x.y“ verweist auf die Zeile in [spec/01-requirements.md](../spec/01-requirements.md); die
dort genannten E2E-Szenarien liegen unter `e2e/platform/specs/`.

### 2.1 Teil A – Grundprinzip und §1–§14

| Element | Stand | Belege | Was fehlt | Paket / PR | Rest | Abh. |
|---|---|---|---|---|---|---|
| [Grundprinzip](../KONZEPT.md#k-gp) | erledigt | R-0.1–R-0.5; ADR-002, ADR-003; E2E-01, -05, -08, -09; `src/agent_hub/`, `src/agent_runner/` | – (CI-Jobs ohne Task: Spannung zu E-34, D-12; öffentliche Landing-Page vor der Anmeldung: E-42) | – | – | E-34, E-42 |
| [§1 P1](../KONZEPT.md#k-1-b1) nur per Einladung | erledigt | R-1.1–R-1.3; E2E-01, -02, -04; `src/agent_hub/invites/` | – | – | – | – |
| [§1 P2](../KONZEPT.md#k-1-b2) Codes | erledigt | R-1.4; E2E-02, -03 | – | – | – | – |
| [§1 P3](../KONZEPT.md#k-1-b3) annehmen, Passkey, anmelden | erledigt | R-1.5–R-1.7, R-1.13; [ADR-055](../adr/055-registration-without-recovery-code-step.md); E2E-01, -30, -39 | angemeldet bleiben: E-40 (in Arbeit) | PR 266 | – | E-40 |
| [§1 P4](../KONZEPT.md#k-1-b4) Geräte mit Code und Geräteschlüssel | erledigt | R-1.8, R-1.9; E2E-04 | – | – | – | – |
| [§1 P5](../KONZEPT.md#k-1-b5) Einmalcodes, Ablauf, Widerruf | erledigt | R-1.10–R-1.12 (übernommen, ADR-005); E2E-03, -04, -05 | – | – | – | – |
| [§2 P1](../KONZEPT.md#k-2-b1) erster Task vorgeschlagen | erledigt | R-2.1; E2E-06 (klickt die Vorschläge, ACC-FINAL Entscheid 6) | – | – | – | – |
| [§2 P2](../KONZEPT.md#k-2-b2) Org aus Einladung | erledigt | R-2.2; E2E-02 | – | – | – | – |
| [§2 P3](../KONZEPT.md#k-2-b3) private Org | erledigt | R-2.3; E2E-01 | – | – | – | – |
| [§2 P4](../KONZEPT.md#k-2-b4) Workgroups, Default-Workgroup | erledigt | R-2.4, R-2.5; E2E-37 | – | – | – | – |
| [§2 P5](../KONZEPT.md#k-2-b5) jede Anfrage ein Task | erledigt | R-2.6–R-2.9; E2E-08 | Erweiterung um erfasste Tasks mit Feldern: E-41 (in Arbeit) | `ad5f2026-e41-build` | – | E-41 |
| [§2 P6](../KONZEPT.md#k-2-b6) keine Projektebene | erledigt | R-2.10; U-HUB-schema (`tests/platform/hub/test_schema.py`) | – | – | – | – |
| [§3 P1](../KONZEPT.md#k-3-b1) physische Geräte | erledigt | R-3.1; E2E-06 | – | – | – | – |
| [§3 P2](../KONZEPT.md#k-3-b2) Geräte zu Org und Workgroups | erledigt | R-3.2, R-3.3; E2E-04, -38, -57 | – | – | – | – |
| [§3 P3](../KONZEPT.md#k-3-b3) DFX-Server, eigene Hardware | erledigt | R-3.4, R-3.5, R-3.14; [ADR-074](../adr/074-own-device-without-terminal.md); E2E-05, -36, -56 | – | – | – | – |
| [§3 P4](../KONZEPT.md#k-3-b4) nur macOS | erledigt | R-3.6; Runner lehnt andere Hosts ab (`unsupported_os`) | – | – | – | – |
| [§3 P5](../KONZEPT.md#k-3-b5) Lima-VM je Workgroup, isolierte Sandboxes, mehrere VMs | erledigt | R-3.7–R-3.9, K-3.5; ADR-008, [ADR-075](../adr/075-resource-choice.md); E2E-10, -11, -57; ATK-01…ATK-40 | – | – | – | – |
| [§3 P6](../KONZEPT.md#k-3-b6) bereit in < 10 s | erledigt | R-3.10; ADR-009; PERF-01, PERF-02 in exklusiven Läufen (FINAL.md) | Restrisiko: Klon-Schritt 5,85–5,96 s von 6 s (FINAL.md). In Arbeit: ein warmer VM-Platz je Gerät über alle Arten (Änderung an ADR-009) | `ad5f2026-pool-one` | – | – |
| [§3 P7](../KONZEPT.md#k-3-b7) Countdown, echter Status | erledigt | R-3.11, R-3.12; E2E-06, -07, -38 | – | – | – | – |
| [§3 P8](../KONZEPT.md#k-3-b8) Serverzuordnung | erledigt | R-3.13 (Rollen Hub- und Runner-Gerät); E2E-32 | Kapazitätsteil durch E-35 ersetzt, dort blockiert | – | – | E-35 |
| [§4 P1](../KONZEPT.md#k-4-b1) nur Claude Code | erledigt | R-4.1; E2E-06 | – | – | – | – |
| [§4 P2](../KONZEPT.md#k-4-b2) eigenes Abo oder API-Key | erledigt | R-4.2, R-4.3; [ADR-090](../adr/090-every-subscription-logged-in-centrally.md); E2E-27, -28 | – | – | – | – |
| [§4 P3](../KONZEPT.md#k-4-b3) Mail-Login-Automatik wiederverwenden | teilweise | R-4.4; ADR-011; `src/agent_runner/login/requests.py`; E2E-28 (nur hermetisch) | Gegen die echten Anmeldeseiten nie live geprüft; dort fällt die Automatik auf das Weiterreichen des Setup-Tokens zurück (ACC-FINAL Entscheid 8, FINAL.md Restrisiken) | `ad5f2026-kor-73` (Anmeldung mit eigenem Postfach und Browser-Sandbox) | S | E-33, E-39; Live-Anmeldung durch eine Person |
| [§4 P4](../KONZEPT.md#k-4-b4) OpenRouter, DeepSeek | erledigt | R-4.5, R-4.6; ADR-010; E2E-27, LIVE-01 | – | – | – | – |
| [§4 P5](../KONZEPT.md#k-4-b5) Kompatibilität prüfen | erledigt | R-4.7; LIVE-01 | – | – | – | – |
| [§4 P6](../KONZEPT.md#k-4-b6) andere Agentensoftware optional | erledigt | R-4.8 (entschieden: nicht in v1, ADR-021) | A38-Slots verschiedener Anbieter: offene Abweichung zu E-32 (KONZEPT Teil D); Modellwahl je Task fehlt (E-42) | – | – | E-32 |
| [§5 P1](../KONZEPT.md#k-5-b1) clientseitig verschlüsselt | erledigt | R-5.1; E2E-09, CON-03 | – | – | – | – |
| [§5 P2](../KONZEPT.md#k-5-b2) Hub sieht keinen Klartext | erledigt | R-5.2, R-5.3; ADR-003, ADR-046; E2E-09 (Canary-Scan), [acceptance/zero-knowledge.md](../acceptance/zero-knowledge.md) | – | – | – | – |
| [§5 P3](../KONZEPT.md#k-5-b3) Entschlüsselung nur durch Berechtigte | teilweise | R-5.4; E2E-18, -31, -72; [key-model.md](../key-model.md) KM-05 | Eine vom Hub erfundene Org auf einem Plattformgerät (KM-31, Lücke KOR-55) | KOR-55 (vom Auftraggeber zurückgestellt, E-25) | M | Auftrag des Auftraggebers |
| [§5 P4](../KONZEPT.md#k-5-b4) Klartext auf DFX-Server | erledigt | R-5.5; ADR-007; E2E-05 | – | – | – | – |
| [§5 P5](../KONZEPT.md#k-5-b5) Schlüsselableitung definiert | erledigt | R-5.6; ADR-006; [key-model.md](../key-model.md); E2E-30, -31 | – | – | – | – |
| [§6 P1](../KONZEPT.md#k-6-b1) lokal speichern, verschlüsselt synchronisieren | erledigt | R-6.1, R-6.2; ADR-022, ADR-030, ADR-050; E2E-14, PERF-03 | – | – | – | – |
| [§6 P2](../KONZEPT.md#k-6-b2) Ereignisarten, Kosten, Kontingente | erledigt | R-6.3–R-6.5; ADR-025, ADR-081; E2E-16 | – | – | – | – |
| [§6 P3](../KONZEPT.md#k-6-b3) Smartphone, präzise Zeiten | erledigt | R-6.6; E2E-29, -58 | – | – | – | – |
| [§6 P4](../KONZEPT.md#k-6-b4) vollständiges Audit | erledigt | R-6.7; ADR-063; E2E-15, -47 | – | – | – | – |
| [§6 P5](../KONZEPT.md#k-6-b5) Append-only Log mit Wiederholung | erledigt | R-6.8, R-6.9; ADR-013; E2E-14, -33 | – | – | – | – |
| [§6 P6](../KONZEPT.md#k-6-b6) Chat als Standardansicht | erledigt | R-6.10; E2E-13 | – | – | – | – |
| [§6 P7](../KONZEPT.md#k-6-b7) Terminalansicht read-only | erledigt | R-6.11, R-6.12; ADR-016; E2E-12, -35 | – | – | – | – |
| [§7 P1](../KONZEPT.md#k-7-b1) Gerät → VM → Session | erledigt | R-7.1; E2E-17 | – | – | – | – |
| [§7 P2](../KONZEPT.md#k-7-b2) Metriken sichtbar und historisiert | erledigt | R-7.2, R-7.3; E2E-17, -47 | – | – | – | – |
| [§7 P3](../KONZEPT.md#k-7-b3) Stack aus DFXswiss/server | erledigt | R-7.4; ADR-018; `deploy/monitoring/` | – | – | – | – |
| [§7 P4](../KONZEPT.md#k-7-b4) getrennt, aber konsistent | teilweise | R-7.5; Runbook [monitoring-patch-handover.md](../runbooks/monitoring-patch-handover.md) | Patch für das Infrastruktur-Repository vorbereitet, nicht eingespielt (R-7.5, CUTOVER S10); das Einspielen ist im Repository nicht belegt | CUTOVER S10 | S | Betreiber |
| [§7 P5](../KONZEPT.md#k-7-b5) Metriken in agent.dfx.swiss | erledigt | R-7.6; E2E-17 | – | – | – | – |
| [§7 P6](../KONZEPT.md#k-7-b6) Engpasswarnung, Verteilung nach Kapazität | erledigt | R-7.7, R-7.8 (Grundversion); E2E-17, -38 | In Arbeit: eine Zulassung je Gerät ohne feste Obergrenzen, Reserve des Hosts (`ad5f2026-workvm-release`); CI im selben Pool: E-35 | `ad5f2026-workvm-release` | – | E-35 |
| [§8 P1](../KONZEPT.md#k-8-b1) Personen auf Org- und Workgroup-Ebene | erledigt | R-8.1, R-8.2; [ADR-060](../adr/060-direct-admission-of-existing-accounts.md); E2E-02, -18, -41 | – | – | – | – |
| [§8 P2](../KONZEPT.md#k-8-b2) Mitglied = Lesen, Admin = Schreiben | erledigt | R-8.3; [ADR-058](../adr/058-admins-read-every-workgroup.md); E2E-02, -18, -45 | – | – | – | – |
| [§8 P3](../KONZEPT.md#k-8-b3) Schreiben in Default-Workgroup „standardmässig“ | teilweise | R-8.4; E2E-18 | Nicht herabsetzbar (A13; key-model KM-32, Lücke KOR-55) | KOR-55 (zurückgestellt, E-25) | S | Auftrag des Auftraggebers |
| [§8 P4](../KONZEPT.md#k-8-b4) Rechte im Backend und in der Oberfläche | erledigt | R-8.5, R-8.6; E2E-18, -45, -72 | – | – | – | – |
| [§8 P5](../KONZEPT.md#k-8-b5) Sessions lesen, was sie dürfen | erledigt | R-8.7, R-8.8; [ADR-089](../adr/089-sessions-read-like-their-human.md); E2E-19, -68 | – | – | – | – |
| [§8 P6](../KONZEPT.md#k-8-b6) Lesezugriffe protokolliert | erledigt | R-8.9; E2E-15, -19, -68 | `/v1/stream` schreibt keine `read.*`-Zeile (erfasste Abweichung, ACC-FINAL Entscheid 8) | – | – | – |
| [§9 P1](../KONZEPT.md#k-9-b1) verbinden und trennen | erledigt | R-9.1; [ADR-065](../adr/065-github-existing-token.md), [ADR-066](../adr/066-held-github-operations.md), [ADR-077](../adr/077-github-every-repository.md); E2E-20, -59 | Gerätefluss gegen GitHub nie live (FINAL.md Restrisiken) | – | – | – |
| [§9 P2](../KONZEPT.md#k-9-b2) Repos, Branches, Commits, PRs, Kommentare | erledigt | R-9.2, R-9.3; E2E-21, LIVE-02 | – | – | – | – |
| [§9 P3](../KONZEPT.md#k-9-b3) Rechte auf Account und Repos, Audit | erledigt | R-9.4, R-9.5; [ADR-054](../adr/054-repository-list-optional.md) (Liste freiwillig, E-11); E2E-22 | – | – | – | – |
| [§9 P4](../KONZEPT.md#k-9-b4) GitHub App | erledigt | R-9.6; ADR-015 | – | – | – | – |
| [§9 P5](../KONZEPT.md#k-9-b5) kein git in der Session | erledigt | R-9.7; E2E-23, ATK-02 | – | – | – | – |
| [§10 P1](../KONZEPT.md#k-10-b1) Harness mit Kontext | erledigt | R-10.1; E2E-23 | – | – | – | – |
| [§10 P2](../KONZEPT.md#k-10-b2) Harness nennt Verbote | erledigt | R-10.2; E2E-23 | – | – | – | – |
| [§10 P3](../KONZEPT.md#k-10-b3) Ausführungsverbot; Subagenten, Tests, Browser nur als Operation | erledigt | R-10.3, R-10.4, R-10.6, K-10.3; [ADR-080](../adr/080-test-runs-as-operations.md), [ADR-085](../adr/085-browser-as-platform-operation.md), [ADR-099](../adr/099-mailboxes-read-by-the-platform.md); `contracts/ops/classes.json` (`test.run`, `browser.open`, `mail.find`); E2E-23, -62, -66, -74 | – (R-16 führt K-10.3 noch als „teils“, Abschnitt 6) | – | – | – |
| [§10 P4](../KONZEPT.md#k-10-b4) keine Umgehung der Hooks | erledigt | R-10.5; E2E-34, ATK-13…ATK-20 | – | – | – | – |
| [§10 P5](../KONZEPT.md#k-10-b5) Hooks und Sandbox | erledigt | R-10.7, R-10.8; ADR-064; ATK-21…ATK-32 | – | – | – | – |
| [§10 P6](../KONZEPT.md#k-10-b6) keine beliebige Shell | erledigt | R-10.9; ADR-012; ATK-33 (Arbeits-VM frei nach E-08) | – | – | – | – |
| [§10 P7](../KONZEPT.md#k-10-b7) formell, nicht inhaltlich | erledigt | R-10.10, R-10.11; E2E-21, -22 | – | – | – | – |
| [§10 P8](../KONZEPT.md#k-10-b8) blockierte Versuche protokolliert | erledigt | R-10.12; KOR-60 (`agent_runner_session_sandbox_denials_total`); E2E-15, -23, -51 | – | – | – | – |
| [§11 P1](../KONZEPT.md#k-11-b1) Datensatz je Operation | erledigt | R-11.1; E2E-21, -22 | – | – | – | – |
| [§11 P2](../KONZEPT.md#k-11-b2) formell prüfen, Rechte, Duplikate | erledigt | R-11.2–R-11.4; ADR-012; E2E-22 | – | – | – | – |
| [§11 P3](../KONZEPT.md#k-11-b3) Kommentar nicht doppelt | erledigt | R-11.5; E2E-22 | – | – | – | – |
| [§11 P4](../KONZEPT.md#k-11-b4) Idempotency-Key | erledigt | R-11.6 (KOR-28); E2E-22 | – | – | – | – |
| [§11 P5](../KONZEPT.md#k-11-b5) abgelehnte und doppelte auditiert | erledigt | R-11.7; E2E-15, -22 | – | – | – | – |
| [§11 P6](../KONZEPT.md#k-11-b6) PR-Erstellung | erledigt | R-11.8; E2E-21, LIVE-02 | – | – | – | – |
| [§11 P7](../KONZEPT.md#k-11-b7) PR-Datenmodell vertagt | erledigt | R-11.9; ADR-014; PR-Index versiegelt ([ADR-084](../adr/084-fix-conflicts-by-merge-commit.md)) | – | – | – | – |
| [§12 P1](../KONZEPT.md#k-12-b1) Whiteboards je Ebene | erledigt | R-12.1, R-12.2; E2E-25 | – | – | – | – |
| [§12 P2](../KONZEPT.md#k-12-b2) Schreiben/Lesen nach Recht | erledigt | R-12.3, R-12.4; E2E-25 | – | – | – | – |
| [§12 P3](../KONZEPT.md#k-12-b3) verschlüsselt, sofort, Historie | erledigt | R-12.5–R-12.7; E2E-25 | – | – | – | – |
| [§12 P4](../KONZEPT.md#k-12-b4) Benachrichtigung | erledigt | R-12.8; ADR-020; E2E-26 | – | – | – | – |
| [§12 P5](../KONZEPT.md#k-12-b5) inhaltsfreier Ping | erledigt | R-12.9; E2E-26 | – | – | – | – |
| [§12 P6](../KONZEPT.md#k-12-b6) Session entscheidet selbst | erledigt | R-12.10; E2E-26 | – | – | – | – |
| [§13.1](../KONZEPT.md#k-13-1) Schlüsselmanagement | erledigt (entschieden) | R-14.1; ADR-006, ADR-007, ADR-058, ADR-060; [key-model.md](../key-model.md) | Lücke KOR-55 siehe §5 P3 | KOR-55 | – | – |
| [§13.2](../KONZEPT.md#k-13-2) Isolation in einer VM | erledigt | R-14.2; ADR-008; ATK-01…ATK-40 | – | – | – | – |
| [§13.3](../KONZEPT.md#k-13-3) Start < 10 s | erledigt | R-14.3; ADR-009, ADR-029; PERF-01, -02 | – | – | – | – |
| [§13.4](../KONZEPT.md#k-13-4) OpenRouter/DeepSeek, Kontingente | erledigt | R-14.4; ADR-010, ADR-025; LIVE-01, LIVE-03 | – | – | – | – |
| [§13.5](../KONZEPT.md#k-13-5) API-Verträge, Idempotenz | erledigt | R-14.5; ADR-012; `contracts/`; CON-01 | – | – | – | – |
| [§13.6](../KONZEPT.md#k-13-6) Gerätezuweisung, Offline-Sync | erledigt | R-14.6; ADR-013, ADR-087; E2E-14, -38, -67 | – | – | – | – |
| [§13.7](../KONZEPT.md#k-13-7) PR-Tabellen später | erledigt (entschieden) | R-14.7; ADR-014 | – | – | – | – |
| [§14 P1](../KONZEPT.md#k-14-b1) DFXswiss/agent | erledigt | R-15.2; [ADR-061](../adr/061-one-repository.md) | – | – | – | – |
| [§14 P2](../KONZEPT.md#k-14-b2) DFXswiss/agent-core | erledigt (ersetzt durch E-22) | R-15.1; ADR-061 (Hub-Code liegt in `src/agent_hub/`) | Stilllegung des alten Hubs: E-32 Schritt 6, CUTOVER S12 | ADR-098 §9 | – | E-32 |
| [§14 P3](../KONZEPT.md#k-14-b3) DFXswiss/server | erledigt | R-15.3; ADR-018 | – | – | – | – |

### 2.2 Teil A – Ideen 26–47

| Element | Stand | Belege | Was fehlt | Paket / PR | Rest | Abh. |
|---|---|---|---|---|---|---|
| [Idee 26](../KONZEPT.md#k-idee-26) kontrollierte Subagenten | erledigt | R-13.1–R-13.8; [ADR-088](../adr/088-full-subagents-and-limits-as-settings.md) (laufen weiter, Grenzen als Einstellungen); `tests/platform/fixed_limits.json`; E2E-24, -68 | – | – | – | – |
| [Idee 27](../KONZEPT.md#k-idee-27) Test-VMs | erledigt | K-27; [ADR-080](../adr/080-test-runs-as-operations.md); `src/agent_runner/workvm/testrun.py`; E2E-62 | – | – | – | – |
| [Idee 28](../KONZEPT.md#k-idee-28) Browser-VMs | erledigt | K-28; [ADR-085](../adr/085-browser-as-platform-operation.md); `src/agent_runner/workvm/browser.py`; E2E-66 | – (R-16 sagt „teils“, Abschnitt 6); Fernsteuerung durch den Menschen ist E-39 | – | – | E-39 |
| [Idee 29](../KONZEPT.md#k-idee-29) Support-Freigabe | erledigt | K-29; [ADR-096](../adr/096-support-release.md); `src/agent_hub/support/`; E2E-72 | Wer die Rolle `support` bekommt und ob ein Agent der Support-Person handeln darf (ADR-096 §6): Entscheide | – | S | Abschnitt 3 |
| [Idee 30](../KONZEPT.md#k-idee-30) Verschlüsselung ohne UX-Nachteil | erledigt | K-30; ADR-055, ADR-058, ADR-082; [key-model.md](../key-model.md) | Lücken KOR-55 siehe §5 P3, §8 P3; Neuladen ohne Passkey: E-40 | PR 266 | – | E-40 |
| [Idee 31](../KONZEPT.md#k-idee-31) Skalierung, Identitätshierarchie | erledigt | K-31; [ADR-095](../adr/095-main-agents-with-names.md), [ADR-086](../adr/086-volume-resilience.md); E2E-71 | – | – | – | – |
| [Idee 32](../KONZEPT.md#k-idee-32) Autonomous-First | erledigt | K-32; [ADR-076](../adr/076-situation-view-and-attention-categories.md); E2E-58 | In Arbeit: offene Tasks als Zahlen auf der Startseite | `ad5f2026-home-counts` | – | – |
| [Idee 33](../KONZEPT.md#k-idee-33) Prozesse optimieren | erledigt | K-33; [ADR-097](../adr/097-process-analysis.md); `src/agent_hub/analysis/`; E2E-73 | Lesezugriffe im Runner und Gast sind nach Release 34 gekommen, also noch nicht live (Abschnitt 5) | – | – | Runner-Release |
| [Idee 34](../KONZEPT.md#k-idee-34) ein Hauptorchestrator je Nutzer | erledigt | K-34; [ADR-056](../adr/056-delegated-agent-access.md), [ADR-082](../adr/082-agent-own-identity.md); E2E-50, -63 | – | – | – | – |
| [Idee 35](../KONZEPT.md#k-idee-35) eigene Hardware für Workgroup oder Org | erledigt | K-35; [ADR-075](../adr/075-resource-choice.md), ADR-074; E2E-56, -57 | – | – | – | – |
| [Idee 36](../KONZEPT.md#k-idee-36) gemeinsame Sessions | erledigt | K-36; ADR-083; E2E-64 | – | – | – | – |
| [Idee 37](../KONZEPT.md#k-idee-37) GitHub-Identitäten | erledigt | K-37; [ADR-078](../adr/078-github-identities.md); E2E-60 | – | – | – | – |
| [Idee 38](../KONZEPT.md#k-idee-38) Ressourcenwahl | erledigt | K-38; [ADR-075](../adr/075-resource-choice.md); E2E-57 | CI-Jobs im selben Pool: E-35/E-36 | – | – | E-35 |
| [Idee 39](../KONZEPT.md#k-idee-39) Abschluss und Cleanup | erledigt | K-39; [ADR-072](../adr/072-session-context-resume.md); E2E-55 | – | – | – | – |
| [Idee 40](../KONZEPT.md#k-idee-40) vollständiges Resume | erledigt | K-40; ADR-072, ADR-093; E2E-55, -69; Wiederaufnahme nach Stromausfall des Runners (Commits nach `011417c0d4`) | – | – | – | – |
| [Idee 41](../KONZEPT.md#k-idee-41) Konflikte beheben | erledigt | K-41; [ADR-084](../adr/084-fix-conflicts-by-merge-commit.md); E2E-65 | – | – | – | – |
| [Idee 42](../KONZEPT.md#k-idee-42) Agentenrollen | erledigt | K-42; [ADR-094](../adr/094-rights-profiles-and-agent-roles.md); E2E-70 | – | – | – | – |
| [Idee 43](../KONZEPT.md#k-idee-43) ein Hauptagent je Task | erledigt | K-43; ADR-072, ADR-089; E2E-55, -68 | E-41 ändert: ein erfasster Task bekommt seinen Agenten erst beim Start (in Arbeit) | `ad5f2026-e41-build` | – | E-41 |
| [Idee 44](../KONZEPT.md#k-idee-44) Wiederherstellung Cloud Sessions | erledigt | K-44; [ADR-087](../adr/087-device-failure.md); `src/agent_hub/failover/`; E2E-67 | – | – | – | – |
| [Idee 45](../KONZEPT.md#k-idee-45) kostenlos bei eigenen Ressourcen | erledigt | K-45; [ADR-062](../adr/062-prepaid-credit-and-usage-billing.md); E2E-46 | – | – | – | – |
| [Idee 46](../KONZEPT.md#k-idee-46) Verbrauchsabrechnung (alle vier Fälle) | erledigt | K-46; ADR-062, ADR-080, ADR-081 §7; [spec/12-billing.md](../spec/12-billing.md) (Browser-VMs wie Lauf-VMs); `src/agent_runner/workvm/browser.py`; E2E-46 | – (R-16 sagt „teils, Browser-VMs nicht“, Abschnitt 6); Abrechnung der CI-Jobs: D-13 | – | – | E-34 |
| [Idee 47](../KONZEPT.md#k-idee-47) Prepaid-Guthaben | erledigt | K-47; ADR-062; `src/agent_runner/billing/pause.py`; E2E-46 | – | – | – | – |

### 2.3 Teil A – §16

| Element | Stand | Belege | Was fehlt | Paket / PR | Rest | Abh. |
|---|---|---|---|---|---|---|
| [§16 P1](../KONZEPT.md#k-16-b1) autonomous-first, Hunderte parallel | teilweise | E-02; ADR-086, ADR-088; Volumentests `web/e2e/volume/`, `scripts/hub_test_volume.py`; Postfach ohne VM (ADR-099) | Abo-Anmeldungen laufen nur nacheinander (ein Postfach je Konto in `src/agent_runner/login/requests.py`) – E-33 | `ad5f2026-kor-73`, `ad5f2026-workvm-release` | M | E-33 |
| [§16 P2](../KONZEPT.md#k-16-b2) nur Org → Workgroup → Task | erledigt | R-0.2, R-2.10; U-HUB-schema | CI-Jobs ohne Task: D-12 | – | – | E-34 |
| [§16 P3](../KONZEPT.md#k-16-b3) frischer Kontext, wiederaufnehmbar | erledigt | ADR-072; E2E-55 | – | – | – | – |
| [§16 P4](../KONZEPT.md#k-16-b4) keine Ausführung, keine Subagenten direkt | erledigt | R-10.3, R-10.4; E-08 (Arbeits-VM); ATK | – | – | – | – |
| [§16 P5](../KONZEPT.md#k-16-b5) Geräteausfall | erledigt | ADR-087; E2E-67 | – | – | – | – |
| [§16 P6](../KONZEPT.md#k-16-b6) nur DFX-Ressourcen kostenpflichtig, Prepaid | erledigt | ADR-062; E2E-46 | – | – | – | – |
| [§16 P7](../KONZEPT.md#k-16-b7) technische Prüfungen | teilweise | Restore über Geräte (ADR-072, ADR-087), Sandbox (ATK), Messung und Abbruch (ADR-062), Start (PERF-01), OpenRouter (LIVE-01), Idempotenz (E2E-22), GitHub-Identitäten (E2E-60), Schlüsselmodell ([key-model.md](../key-model.md)) | Schlüsselmodell mit Lücke KOR-55 (zurückgestellt); Live-Nachweise offen: Gerätefluss, Abo-Erfolgspfad, PRF-Gerätematrix (FINAL.md Restrisiken) | KOR-55 | M | Auftrag; Person für Live-Prüfungen |

### 2.4 Teil B – Regeln E-01 bis E-36 auf `develop`

| Element | Stand | Belege | Was fehlt | Paket / PR | Rest | Abh. |
|---|---|---|---|---|---|---|
| [E-01](../KONZEPT.md#e-01) Konzeptfassung maßgeblich | erledigt | Block R-16; `tests/platform/test_konzept.py` (`test_part_a_has_every_section_and_idea`) | – | – | – | – |
| [E-02](../KONZEPT.md#e-02) keine künstliche Obergrenze | erledigt | [ADR-086](../adr/086-volume-resilience.md) (KOR-35), [ADR-088](../adr/088-full-subagents-and-limits-as-settings.md) (KOR-45), ADR-075 (Warten statt Ablehnung), ADR-081; `tests/platform/fixed_limits.json` | – (KONZEPT und R-E: „offen/teils“, Abschnitt 6). In Arbeit: feste Grenzen der gleichzeitigen VMs entfallen | `ad5f2026-workvm-release` | – | – |
| [E-03](../KONZEPT.md#e-03) nur Passkey, Rest macht der Agent | erledigt | ADR-056, ADR-067; Anleitung `agents.md` (KONZEPT E-03); E2E-49, -50 | – | – | – | – |
| [E-04](../KONZEPT.md#e-04) Gleichstellung Agentenzugang | erledigt | ADR-056; [agent-parity.md](../agent-parity.md); `src/agent_platform/` | gilt für jede neue Fähigkeit (Arbeitsweisen, erfasste Tasks, Fernsteuerung) | – | – | – |
| [E-05](../KONZEPT.md#e-05) Voraussetzungen anstoßen | erledigt | ADR-066, ADR-079; [spec/15-prerequisites.md](../spec/15-prerequisites.md); E2E-61 | – | – | – | – |
| [E-06](../KONZEPT.md#e-06) GitHub über Betreiberkonto | erledigt | ADR-053, ADR-054, ADR-065, ADR-078 | – | – | – | – |
| [E-07](../KONZEPT.md#e-07) Betreiber-Abos | erledigt | [ADR-052](../adr/052-operator-subscriptions.md), ADR-062 | Liste der Organisationen: Entscheid (siehe E-11) | – | – | – |
| [E-08](../KONZEPT.md#e-08) Arbeits-VM | erledigt | [ADR-059](../adr/059-work-vm-on-request.md); `src/agent_runner/workvm/manager.py`; E2E-48 | – | – | – | – |
| [E-09](../KONZEPT.md#e-09) nichts überwacht in der VM | erledigt | ADR-059 | – | – | – | – |
| [E-10](../KONZEPT.md#e-10) Ausgang nie über Standortadresse | erledigt | ADR-059 (geteilte Adressen, fail-closed, nur kostenlose Tarife) | CI-Ausgang: D-8 | – | – | E-34 |
| [E-11](../KONZEPT.md#e-11) nichts einzeln freigeben | blockiert | ADR-054, ADR-059, [ADR-079](../adr/079-no-approval-as-precondition.md) (Register mit Prüfung, vom Root signierte Schalter `open_platform_access`, `open_platform_invites`); E2E-61 | Schalter umlegen; Liste der Organisationen für Betreiber-Abos | – | S | Entscheid des Auftraggebers |
| [E-12](../KONZEPT.md#e-12) nur PRF-Passkey | erledigt | ADR-055; E2E-30 | PRF-Gerätematrix manuell (FINAL.md) | – | – | – |
| [E-13](../KONZEPT.md#e-13) kein Wiederherstellungscode-Schritt | erledigt | ADR-055; E2E-01 | – | – | – | – |
| [E-14](../KONZEPT.md#e-14) Guthaben nur vom Betreiber | erledigt | ADR-062; [runbooks/credit.md](../runbooks/credit.md) | Guthaben vor dem Einschalten der Abrechnung verteilen (Betrieb) | – | – | – |
| [E-15](../KONZEPT.md#e-15) Marktdurchschnitt in Konfiguration | erledigt | ADR-062 | – | – | – | – |
| [E-16](../KONZEPT.md#e-16) alles in USD | erledigt | ADR-062 (Mikro-USD) | – | – | – | – |
| [E-17](../KONZEPT.md#e-17) CHF-Gegenwert je Buchung | erledigt | ADR-062 | – | – | – | – |
| [E-18](../KONZEPT.md#e-18) direkte Aufnahme | erledigt | ADR-060, ADR-073; E2E-41 | – | – | – | – |
| [E-19](../KONZEPT.md#e-19) Fehlerklassen ausschließen | erledigt (Grundsatz) | erster Fall gebaut: ADR-086 mit Volumen-Gate (`web/e2e/volume/completeness.ts`); Register mit Vollständigkeitsprüfung (ADR-079) | gilt für jedes Paket (KONZEPT: „teils, KOR-35 offen“, Abschnitt 6) | – | – | – |
| [E-20](../KONZEPT.md#e-20) Community | erledigt | [ADR-069](../adr/069-community-chat.md); [spec/14-community.md](../spec/14-community.md); `src/agent_hub/community/`, `web/src/ui/screens/community/`, `tests/platform/hub/community/` | – (KONZEPT und R-E: „offen“, Abschnitt 6) | – | – | – |
| [E-21](../KONZEPT.md#e-21) Menüpunkt Tasks | erledigt | KOR-32 ([decisions/KOR-32.md](../decisions/KOR-32.md)) | Filter nach den Feldern erfasster Tasks: E-41 | `ad5f2026-e41-build` | – | E-41 |
| [E-22](../KONZEPT.md#e-22) ein Repository | erledigt | ADR-061 | – | – | – | – |
| [E-23](../KONZEPT.md#e-23) zentrale Datei | erledigt | KONZEPT.md; `tests/platform/test_konzept.py` | – | – | – | – |
| [E-24](../KONZEPT.md#e-24) kein vorgeschalteter Zugangsschutz | erledigt (Betrieb) | KONZEPT „gebaut (Betrieb)“; im Repository nur mittelbar prüfbar | – | – | – | – |
| [E-25](../KONZEPT.md#e-25) keine unbestellten Schutzschichten | erledigt (Grundsatz) | zurückgestellt: KOR-55, zwei Abschottungen (KONZEPT E-25), Gate U (spec/16 §9.9) | – | – | – | – |
| [E-26](../KONZEPT.md#e-26) einfache Bedienung durch Agenten | erledigt | [decisions/KOR-63.md](../decisions/KOR-63.md); `src/agent_platform/connector.py`, `src/agent_platform/guide.md`; `tests/platform/tool/test_mcp.py` | – (R-E: „offen“, Abschnitt 6) | – | – | – |
| [E-27](../KONZEPT.md#e-27) Agent ohne Mensch | erledigt | [ADR-071](../adr/071-agent-own-account.md); E2E-54 | – | – | – | – |
| [E-28](../KONZEPT.md#e-28) persönliche Nachricht | erledigt | [decisions/KOR-64.md](../decisions/KOR-64.md); `web/src/ui/screens/connect/RequesterMessage.tsx`, `contracts/log/invitation-package.json` | – (R-E: „offen“, Abschnitt 6) | – | – | – |
| [E-29](../KONZEPT.md#e-29) GitHub mit jedem Repository | erledigt | [ADR-077](../adr/077-github-every-repository.md); E2E-59 | – | – | – | – |
| [E-30](../KONZEPT.md#e-30) eigene Abos auf einen Blick | erledigt | [ADR-091](../adr/091-subscriptions-at-a-glance.md); `web/src/core/subscriptions/subscriptions.ts` (`totalWindows`: Gesamtstand aller Konten), `web/src/ui/screens/subscriptions/` | Erweiterungen in Arbeit: Bindungsfenster und Rest je Konto (PR 265), Verbrauch je Person (`ad5f2026-subs-usage`), Sitzung übersteht Erneuerung des Abos (`ad5f2026-hot-26`). PR 264 (Gesamtstand) ist noch offen, sein Inhalt steht aber schon auf `develop`; zu prüfen, ob er sich erledigt hat | PR 265, PR 264 | S | – |
| [E-31](../KONZEPT.md#e-31) alle Abos zentral | erledigt | [ADR-090](../adr/090-every-subscription-logged-in-centrally.md), ADR-092 | parallele Anmeldung: E-33 | – | – | E-33 |
| [E-32](../KONZEPT.md#e-32) jeder lokale Baustein wählbare Arbeitsweise | in Arbeit | Entwurf [ADR-098](../adr/098-working-modes.md), A19, [decisions/KOR-72.md](../decisions/KOR-72.md); auf `develop` noch kein Code (kein `contracts/modes/`, kein `task.mode` in `contracts/ops/classes.json`); Schritt 1 (Grundlage der Arbeitsweisen) auf `ad5f2026-kor-75` | ADR-098 §9: Schritt 1 fertigstellen; dann Schritte 2–6 (`orchestrator`, `mac_checks`, `a38`, `subscription_login`, `merge_lane`, `notifications`, `coordinator`, `prod_error_fix`, `release_ops`, Stilllegung von Altwerkzeug und altem Hub) | `ad5f2026-kor-75`; `ad5f2026-doc-10` (§7) | L | D7, D9; doc-10 mergen; KOR-73 für `subscription_login`; CUTOVER S12 |
| [E-33](../KONZEPT.md#e-33) parallele Vorgänge, eigene Sandbox | in Arbeit | auf `develop`: Postfach des Inhabers in der Anmeldung (`src/agent_runner/login/requests.py`), mehrere Postfächer je Mitglied seit ADR-099; `ad5f2026-kor-73`: Abos nebeneinander anmelden, je mit eigenem Postfach und eigener Browser-Sandbox, Prüfung für Menschen durch den Inhaber, Registrierung neuer Konten | Merge von KOR-73 (Ausrichtung an ADR-099, decisions KOR-74 Punkt 1); Auslieferung | `ad5f2026-kor-73` | M | E-37 (gebaut), E-39 |
| [E-34](../KONZEPT.md#e-34) alle selbst gehosteten Actions-Läufe über die Plattform | blockiert | Konzept [spec/16-ci-runners.md](../spec/16-ci-runners.md); A20; kein Code (keine JIT- oder Scale-Set-Anbindung im Repo) | ganzer CI-Dienst (Stufen 0–7, spec/16 §12.2), Messungen M-1…M-21 | Paket nicht vergeben | L | D-1–D-4, D-6, D-8, D-9, D-12, D-13, D-20 |
| [E-35](../KONZEPT.md#e-35) eine gemeinsame Kapazität | blockiert | spec/16 §6.6, §12.3; Kapazität zählt heute nur RAM (KONZEPT Teil D); eine Zulassung je Gerät in Arbeit (`ad5f2026-workvm-release`) | Zulassung nach §6.6 für CI, Umzug der CI-Rechner (§12.3 a–f), gerätegebundene Läufe | Paket nicht vergeben | L | D-1, D-22, D-23, D-25; E-34 |
| [E-36](../KONZEPT.md#e-36) CI-Jobs auf denselben Geräten | blockiert | entscheidet D-5 (spec/16 §13) | Gate U oder nicht, Anteile, Klasse U neben T/P | Paket nicht vergeben | M | D-7, D-20, D-22, D-24; E-34, E-35 |

### 2.5 Teil B – Regeln und Entscheide auf Branches

Die Regeln stehen auf `ad5f2026-doc-11` (ein Commit vor `develop`, „Record rules E-37 to E-42 and interpretations A22
and A23“) und sind dort schon fortlaufend nach E-36 nummeriert. Sie verlinken noch nicht in KONZEPT.md auf
`develop`; die Spalte „Element“ nennt deshalb Nummer und Titel.

| Element | Stand | Belege | Was fehlt | Paket / PR | Rest | Abh. |
|---|---|---|---|---|---|---|
| ADR-098 §7 – Zugänge D1–D9 (Branch `ad5f2026-doc-10`) | in Arbeit | Branch (ein Commit, 36 hinter `develop`): D1–D6 und D8 als entschieden eingetragen (Wortlaut zu E-32 „alles sofort umziehen, ohne zu fragen“), D7 und D9 „owner decides“; auf `develop` sind alle neun noch offen | auf `develop` bringen und mergen; D7, D9 entscheiden | `ad5f2026-doc-10` (ohne PR) | S | D7, D9 |
| E-37 – Was eine Plattform-Operation sein kann, braucht keine VM | erledigt | auf `develop` gebaut (Umsetzungszeile auf dem Branch noch „offen“): [ADR-099](../adr/099-mailboxes-read-by-the-platform.md), [decisions/KOR-74.md](../decisions/KOR-74.md); Operation `mail.find` (`contracts/ops/classes.json`), Abschnitt „Postfächer“ in den Einstellungen, Befehle im Agentenwerkzeug; `e2e/platform/specs/e2e-74-mailbox.spec.ts` | Regel mergen; Auslieferung (nach `9273856bb` bzw. Release 34 gekommen) | `ad5f2026-doc-11` | S | Hub- und Runner-Release |
| E-38 – Eigener Tresor nur für Agenten | nur Konzept | Branch: Konzept „ADR-099 agent vault“, Status „Proposed“ | ausdrücklich nicht beauftragt („nur als Konzept anlegen, noch nicht umsetzen“); ADR-Nummer 099 ist auf `develop` vergeben (Postfächer) | – | L | Auftrag des Auftraggebers; E-37 |
| E-39 – Prüfungen für Menschen per Fernsteuerung | in Arbeit | `ad5f2026-vnc` („Remote control by VNC when a process needs a human (ADR-101)“, 26 vor, 21 hinter `develop`); `ad5f2026-kor-73` (Prüfung durch den Inhaber bei der Abo-Anmeldung); vorhanden ist das Live-Bild der Browser-VM ohne Eingabeweitergabe (ADR-085, `src/agent_runner/crypto/browserframe.py`) | Merge und Auslieferung; danach Fernsteuerung für alle Browser-Sandboxen der Sitzungen (laut Regel „folgt“) | `ad5f2026-vnc`, `ad5f2026-kor-73` | M | E-33 |
| E-40 – Angemeldet bleiben | in Arbeit | PR 266 (Entwurf, `ad5f2026-stay-signed-in`, 3 vor, 23 hinter `develop`): `POST /v1/session/renew`, Gerätschlüssel des Browsers, Schalter auf der Anmeldeseite und in den Einstellungen | Nummern angleichen (PR nennt E-37, ADR-099, KOR-74 – alle drei anders vergeben), auf `develop` bringen, prüfen, mergen, ausliefern | PR 266 | S | Umnummerierung |
| E-41 – Tasks erfassen wie Tickets in Jira (mit A22) | in Arbeit | `ad5f2026-e41-build` (13 Commits: Task-Übersicht über alle Organisationen mit Zählern, Filtern und Reihenfolge) | Felder Person, Thema, GitHub-Organisation, Repository, Dringlichkeit, Status todo/inProgress/completed; Erfassen ohne Agent, Start durch Person; Werkzeug (E-04) – Umfang des Branches gegen die Regel zu prüfen | `ad5f2026-e41-build` | M | Idee 43 und §2 P5 werden geändert |
| E-42 – Landing-Page wie dfx.swiss, gebaut mit Fable 5.2 (mit A23) | in Arbeit | Regel: „in Arbeit – Aufgabe auf agent mit Fable 5.2“; unter den Branches vom 26.11. keiner erkennbar zugeordnet | Seite selbst; die Plattform kennt keine Modellwahl je Task (laut Regel) | Aufgabe auf agent | M | Modellwahl je Task (Lücke) |

## 3. Offene Entscheidungen des Auftraggebers

| Entscheid | Quelle | Blockiert |
|---|---|---|
| D7 – GitHub-Konten über das eigene hinaus (Zweitprüfung im Team, Worker-Logins des Koordinators) | [ADR-098](../adr/098-working-modes.md) §7 | Arbeitsweisen `a38` (Zweitprüfung), `coordinator` (E-32 Schritte 3–5) |
| D9 – Rebase-Zug mit `--force-with-lease` statt Merge-Commits | ADR-098 §7, A2 | nur die Variante von `merge_lane`; ohne Entscheid gilt A2 (Merge-Commits), Schritt 3 ist baubar |
| D1–D6, D8 als entschieden festhalten | Branch `ad5f2026-doc-10` | auf `develop` stehen sie noch als offen; Merge des Branches (kein Entscheid mehr nötig, nur Übernahme) |
| D-1 – Umfang; gerätegebundene Läufe (Deploy) außerhalb der Plattform oder Pull-Deployment | [spec/16 §13](../spec/16-ci-runners.md#13-open-decisions) | E-34, E-35 (Umzug Schritt e), offene Abweichung in KONZEPT Teil D |
| D-2 Auslöser, D-3 Ort des Controllers, D-4 Eigentümer von CI-App und Schlüssel | spec/16 §13 | Bau des CI-Dienstes (Stufe 1) |
| D-7 und D-20 – Gate U, Härtungsauftrag (E-25) | spec/16 §13, §9.3, §9.9 | Klasse U (PR-Code) ab Stufe 3; E-36 |
| D-9 GitHub-Plan, D-12 Modell im KONZEPT (CI-Job ohne Task/Workgroup), D-13 Abrechnung | spec/16 §13 | Trennung der Klassen; Audit- und Abrechnungsmodell der CI-Jobs |
| D-6 Fork-PRs, D-8 Ausgang der CI-VMs (nur kostenlose Tarife, E-10) | spec/16 §13 | Stufe 2 und folgende |
| D-10, D-11, D-14, D-15, D-18, D-22, D-23, D-24, D-26 | spec/16 §13 | Feinplanung; meist erst nach den Messungen M-5, M-8, M-9, M-19, M-20 entscheidbar |
| D-16, D-17, D-19, D-21 – macOS (Eigennutzung, RealUnit, Werkzeug, rechtliche Prüfung) | spec/16 §13, §8 | macOS-CI; berührt `mac_checks` von E-32 |
| D-25 – welche Betreiber-Rechner in den Pool kommen | spec/16 §13 | E-35 Umzug |
| Welche Schalter umgelegt werden (`open_platform_access`, `open_platform_invites`), welche Organisationen die Betreiber-Abos nutzen | KONZEPT Teil D (E-11), [ADR-079](../adr/079-no-approval-as-precondition.md) | Rest von E-11 |
| KOR-55 beauftragen oder nicht (Vertrauensanker des Runners) | [key-model.md](../key-model.md), E-25 | §5 P3, §8 P3 (A13), §16 P7 |
| Wer die Rolle `support` bekommt; darf ein Agent der Support-Person handeln | [ADR-096](../adr/096-support-release.md) §6 | Betrieb der Support-Freigabe (Idee 29) |
| Agent-Tresor beauftragen (E-38) | Branch `ad5f2026-doc-11` | Tresor |
| Stilllegung des alten Hubs (CUTOVER S12, mit Zustimmung) | [CUTOVER.md](../CUTOVER.md) §7, ADR-098 §6 | E-32 Schritt 6 |

Die Auslegungen A1–A23 (KONZEPT Teil C, A22 und A23 auf dem Branch) gelten, bis der Auftraggeber anders entscheidet;
sie blockieren nichts.

## 4. Kritische Kette der offenen Arbeit

Reihenfolge nach Abhängigkeit, ohne Termine.

1. **Nummern und Doku bereinigen (S, ohne Abhängigkeit, vor jedem weiteren Merge).** `ad5f2026-doc-11` (E-37 bis
   E-42) mergen und dabei die Umsetzungszeile von E-37 auf „gebaut“ setzen; das Tresor-Konzept bekommt eine freie
   ADR-Nummer (099 ist das Postfach). PR 266 übernimmt die Regel E-40 und bekommt eine freie ADR- und Paketnummer
   (statt E-37, ADR-099, KOR-74). `ad5f2026-doc-10` auf `develop` bringen und mergen. Teil D von KONZEPT.md, R-16 und
   R-E an den gebauten Stand anpassen (Abschnitt 6).
2. **Auslieferung (S–M).** Runner und Gast stehen auf Release 34. Was danach kam (Analyse-Lesezugriffe für Idee 33,
   Postfach E-37, Wiederaufnahme nach Stromausfall, Golden-Wechsel-Korrektur), wirkt erst nach einem Runner-/Gast-Release
   und einer Hub-Auslieferung nach `9273856bb` (Abschnitt 5). Jeder Merge der folgenden Ketten verlängert diese Liste.
3. **Parallele Plattform-Vorgänge (M).** E-37 (gebaut) → E-33 `ad5f2026-kor-73` (eigenes Postfach und eigene
   Browser-Sandbox je Abo, angeglichen an das Postfach-Lager von ADR-099) → E-39 Fernsteuerung (`ad5f2026-vnc`, ADR-101)
   zuerst in der Abo-Anmeldung, dann für alle Browser-Sandboxen → Live-Nachweis der Anmeldung (§4 P3). Parallel dazu
   die Zulassung je Gerät ohne feste Obergrenzen (`ad5f2026-workvm-release`) und ein warmer VM-Platz je Gerät
   (`ad5f2026-pool-one`). Danach ist [§16 P1](../KONZEPT.md#k-16-b1) erledigt.
4. **Arbeitsweisen (E-32, L)** nach ADR-098 §9:
   Schritt 1 Grundlage (`ad5f2026-kor-75`: Katalog, `task.mode` mit Zitatprüfung, Oberfläche, Werkzeug, MCP, Treiber) →
   Schritt 2 `orchestrator` Plan, `mac_checks` (D8), `a38` Testbericht, `subscription_login` (setzt Kette 3 voraus) →
   Schritt 3 `orchestrator` Helfer, `merge_lane` (A2; D9 nur für den Rebase-Zug), `a38` Review und Ready (D7 für
   die Team-Zweitprüfung), `notifications` (D4, D5) →
   Schritt 4 `coordinator` (D7), `prod_error_fix` (D3), `release_ops` (D1, D2) →
   Schritt 5 `coordinator` Duplikate und Konflikte →
   Schritt 6 Altwerkzeug stilllegen (Importe zuerst umziehen) und alten Hub stilllegen (CUTOVER S12, Zustimmung).
   Offene PRs zum Altwerkzeug (202, 204, 205, 206, 223, 256) betreffen Bausteine, die dieser Weg ablöst.
5. **Erfasste Tasks und Landing-Page (je M, unabhängig).** E-41 (`ad5f2026-e41-build`, ändert Idee 43 und §2 P5);
   E-42 (braucht für „mit Fable 5.2“ als Plattformfunktion eine Modellwahl je Task, die es nicht gibt).
6. **CI über die Plattform (E-34–E-36, L):** Entscheide D-1–D-4, D-9, D-12, D-13 → Stufe 0 Inventur → Messungen
   (M-1…M-9) → Stufe 1 Testorganisation mit Zulassung nach §6.6 (baut auf der Zulassung je Gerät aus Kette 3) →
   Stufe 2 Schattenbetrieb (M-19, M-20) → Gate U nach D-7/D-20 → Stufen 3–6 → Umzug der CI-Rechner in den Pool
   (§12.3, D-1, D-25) → macOS nach D-21 und Spike (berührt `mac_checks`). Gerätegebundene Läufe bleiben bis D-1 offene
   Abweichung.
7. **Nur nach Auftrag:** KOR-55 (Vertrauensanker; schließt §5 P3, §8 P3, Teil von §16 P7) und der Agent-Tresor E-38
   (setzt E-37 voraus).

Gemeinsame Engstellen: die Abo-Anmeldung (Kette 3 und Kette 4 Schritt 2), die Kapazitätszulassung (Kette 3 und Kette
6) und macOS (Kette 4 `mac_checks` und Kette 6 macOS-CI). Schritt 1 und 2 der Liste gehen jeder Auslieferung voraus.

## 5. Produktionsstand

Live sind Hub und Weboberfläche auf `9273856bb`, Runner und Gast auf Release 34 (`682fc189`). Aus der Commit-Folge
von `develop` (erste Eltern) ergibt sich:

- **Hub und Weboberfläche:** Nach `9273856bb` kamen das CI-Konzept (PR 261, PR 267; nur Dokumentation), das Postfach
  als Plattform-Werkzeug (E-37: Hub-Lager, Operation `mail.find`, Einstellungsabschnitt „Postfächer“, E2E-74) und die
  Wiederaufnahme nach einem Stromausfall des Runners. E-37 ist also gebaut, aber nicht live.
- **Runner und Gast:** Nach Release 34 kamen außerdem die Lesezugriffe der Prozessanalyse in Runner und Gast (Idee 33,
  ADR-097; Gastversion 0.6.0), die Korrektur des Golden-Wechsels (ADR-093), die Postfach-Operation im Runner und im Gast
  (Gastversion 0.7.0) sowie die Wiederaufnahme nach einem Stromausfall. Die Prozessanalyse ist damit in der
  Weboberfläche sichtbar, ihre Lesezugriffe in der Session funktionieren aber erst mit dem nächsten Runner-/Gast-Release.
- Alle anderen als „erledigt“ geführten Elemente lagen vor diesen Commits auf `develop`.
- Unabhängig davon stehen die Schritte S10 (Monitoring-Patch) und S12 (alter Hub) des
  [Umstellungsplans](../CUTOVER.md) aus.

## 6. Abweichungen der Dokumentation vom gebauten Stand

Nur benannt, nicht geändert (Auftrag: keine Regel- oder Umsetzungszeilen umschreiben).

| Stelle | Steht dort | Gebauter Stand | Beleg |
|---|---|---|---|
| KONZEPT [E-02](../KONZEPT.md#e-02), [E-19](../KONZEPT.md#e-19) Umsetzung; Teil D „E-19, E-02 Datenmenge“ | KOR-35 offen, KOR-45 offen | gebaut | ADR-086, ADR-088; `web/e2e/volume/`, `tests/platform/fixed_limits.json` |
| KONZEPT [E-20](../KONZEPT.md#e-20); Teil D „E-20 Community“; R-E E-20 | offen – KOR-26 | gebaut | ADR-069; `src/agent_hub/community/`, `web/src/ui/screens/community/`, `tests/platform/hub/community/` |
| KONZEPT Teil D A5/A14, A10, A11 | feste Grenzen; Subagent endet nach einem Turn; liest nur eigene Workgroup | gebaut | ADR-088, ADR-089; R-13.4, R-13.8, R-8.7; E2E-24, -68 |
| KONZEPT Teil D A8/A9 | kein Testlauf als Operation | gebaut | ADR-080; K-27; E2E-62 |
| KONZEPT Teil D A18 | Kernel-Ablehnungen nicht gezählt | gebaut | R-10.12 (KOR-60); E2E-51 |
| R-E E-02, E-19 | teils | gebaut | wie oben |
| R-E E-26, E-28 | offen | gebaut (KONZEPT sagt „gebaut“) | decisions KOR-63, KOR-64 |
| R-16 K-10.3 | teils, „tests and browsers not yet“ | gebaut | ADR-080, ADR-085 |
| R-16 K-28 | teils | gebaut; kein Rest benannt | ADR-085; E2E-66 |
| R-16 K-46 | teils, „browser VMs … not yet“ | gebaut (Browser-VM wie Lauf-VM abgerechnet) | spec/12 „Browser VMs“; `src/agent_runner/workvm/browser.py` |
| R-8.4 | „not reducible in v1 (ADR-024 addendum)“ | stimmt, ADR-024 ist aber durch ADR-089 ersetzt; Rest ist KOR-55 (A13) | ADR-089 Status |
| Branch `ad5f2026-doc-11` E-37 | „Umsetzung: offen – Postfach als Werkzeug in KOR-74“ | gebaut auf `develop` | ADR-099, E2E-74 |
| [adr/README.md](../adr/README.md) Zeile ADR-096 | Linktext „090“ | Datei `096-support-release.md` | – |
| Nummern auf Branches | E-37 auf doc-11 (Plattform-Werkzeug) und in PR 266 (angemeldet bleiben); ADR-099 auf `develop` (Postfächer), auf doc-11 (Tresor) und in PR 266; KOR-74 auf `develop` (Postfächer) und in PR 266 | Kollisionen; doc-11 führt „angemeldet bleiben“ als E-40 | Abschnitt 2.5 |
| Umzugsplan „P1–P12“ zu E-32 | im Auftrag genannt | im Repository nicht vorhanden (KOR-72 nennt ein internes Inventar); die Bauordnung im Repository ist ADR-098 §9, Schritte 1–6 | [decisions/KOR-72.md](../decisions/KOR-72.md) |
