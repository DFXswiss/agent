# agent.dfx.swiss – Konzept und verbindliche Regeln

Diese Datei ist die **erste Quelle** für jeden – Mensch oder KI-Agent –, der an agent.dfx.swiss arbeitet. Sie enthält
das Konzept des Auftraggebers (Teil A), seine späteren Regeln und Entscheide (Teil B), die Auslegungen, die gelten,
bis er anders entscheidet (Teil C), und die Regeln zur Pflege dieser Datei (Teil D). Spezifikation
([spec/](spec/00-overview.md)), Architekturentscheide ([adr/](adr/README.md)) und Code leiten sich hieraus ab; die
Rückverfolgung je Anforderung steht in [spec/01-requirements.md](spec/01-requirements.md).

**Rangfolge.** Teil B ist jünger als Teil A: Widerspricht eine Regel aus Teil B dem Konzept, gilt die Regel (die
jüngere ausdrückliche Aussage des Auftraggebers). Teil C gilt nur, solange der Auftraggeber nichts anderes
entscheidet. Nichts in Spezifikation, ADRs oder Code darf diesen Teilen widersprechen; bekannte, noch nicht
umgesetzte Abweichungen stehen in [Teil D](#d-offen) als „offen“.

**Anker.** Jede Stelle hat einen stabilen Anker, auf den Spezifikation und ADRs verweisen:
`#k-gp` (Grundprinzip), `#k-3` (Abschnitt 3), `#k-3-b5` (Abschnitt 3, fünfter Punkt), `#k-13-2` (Abschnitt 13,
Nummer 2), `#k-idee-26` (Idee 26), `#k-idee-46-b2` (Idee 46, zweiter Punkt), `#k-16-b4` (Abschnitt 16, vierter
Punkt), `#e-01` (Regel E-01), `#a-1` (Auslegung A1). Anker werden nie umbenannt oder neu vergeben.

| Teil | Inhalt |
|---|---|
| [A – Das Konzept](#teil-a--das-konzept) | Vollständige Konzeptfassung mit 47 Ideen, im Wortlaut |
| [B – Regeln und Entscheide](#teil-b--regeln-und-entscheide-des-auftraggebers) | Regeln E-01 … mit Datum, Wortlaut, Bezug und Umsetzung |
| [C – Auslegungen](#teil-c--auslegungen-bis-der-auftraggeber-anders-entscheidet) | A1–A18 |
| [D – Pflege](#teil-d--pflege) | Wie eine neue Vorgabe eingetragen wird; offene Abweichungen |

## Teil A – Das Konzept

Wortlaut der abgeschlossenen Konzeptfassung des Auftraggebers (47 Ideen, Stand 21. November 2026), unverändert bis
auf die Anker, die Überschriftenebenen und eine kenntlich gemachte Kürzung (Servernamen, [Teil D](#d-kuerzungen)).
Die Ideen 26–47 sind im Original nummeriert; Abschnitte 1–14 und 16 tragen ihre Abschnittsnummer.

**Frühere Nummerierung.** Bis zum 23. November 2026 wurde nach einer Kurzfassung (Ideen 1–26) gebaut. Ältere Texte,
die „KONZEPT §13“, „§14.N“ oder „§15“ ohne Verweis nennen, meinen diese Kurzfassung. Zuordnung:
Kurzfassung §1–§12 = hier §1–§12 (in §10 entspricht der fünfte Punkt der Kurzfassung, die Präzisierung zu
Subagenten, dem zweiten Satz von [§10 Punkt 3](#k-10-b3); die Punkte 6–9 der Kurzfassung sind hier 5–8) ·
Kurzfassung §13 „Kontrollierte Subagenten“ = hier [Idee 26](#k-idee-26) · Kurzfassung §14.N = hier
[§13.N](#k-13) · Kurzfassung §15 = hier [§14](#k-14). Die ADRs 001–056 sind Entscheidungsprotokolle und behalten diese Nummern;
neue ADRs verweisen mit einem Link auf einen Anker dieser Datei.

<a id="k-titel"></a>**agent.dfx.swiss – Produkt- und Architekturanforderungen**

Stand: 21. November 2026  
Status: Abgeschlossene Konzeptfassung (47 Ideen). Verbindliche Anforderungen und offene technische Umsetzungsfragen sind getrennt.

<a id="k-gp"></a>

### Grundprinzip
Eine einladungsbasierte, verschlüsselte Plattform für KI-gestützte Aufgaben. Die Hierarchie besteht **ausschliesslich** aus **Organization → Workgroup → Task**. `agent-core` koordiniert und synchronisiert; KI-Sessions laufen auf zugewiesenen Ausführungsgeräten. Der Einstieg soll ohne Konfiguration möglich sein.

<a id="k-1"></a>

### 1. Einladung und Identität
- <a id="k-1-b1"></a>Neue Nutzer, Kunden und Geräte dürfen ausschliesslich per Einladung hinzugefügt werden.
- <a id="k-1-b2"></a>Einladungen werden als Codes generiert und weitergegeben.
- <a id="k-1-b3"></a>Nutzer nehmen die Einladung an, richten einen Passkey ein und melden sich auf `agent.dfx.swiss` an.
- <a id="k-1-b4"></a>Geräte werden über einen Einladungscode gekoppelt; anschliessend authentifizieren sie sich kryptografisch über einen gerätespezifischen Schlüssel, ähnlich dem SSH-Schlüsselprinzip, ohne SSH vorauszusetzen.
- <a id="k-1-b5"></a>**Vorschläge, noch nicht beschlossen:** Einmalcodes, Ablaufzeiten und Widerruf/Entkopplung.

<a id="k-2"></a>

### 2. Onboarding und erster Task
- <a id="k-2-b1"></a>Nach Passkey-Login wird unmittelbar eine erste Aufgabe vorgeschlagen, etwa eine Recherche oder ein GitHub-Pull-Request.
- <a id="k-2-b2"></a>Ist in der Einladung eine Organization angegeben, wird diese zum Standardkontext.
- <a id="k-2-b3"></a>Andernfalls wird automatisch eine private Organization erstellt.
- <a id="k-2-b4"></a>Jede Organization besitzt Workgroups; ohne explizite Auswahl wird die Default-Workgroup verwendet.
- <a id="k-2-b5"></a>**Jede Nutzeranfrage**, auch eine einfache Wissensfrage, erzeugt automatisch einen Task mit eindeutiger ID, Kurzbeschreibung und Workgroup-Zuordnung.
- <a id="k-2-b6"></a>Hierarchie: **Organization → Workgroup → Task**. Eine separate Projektebene existiert **nicht**.

<a id="k-3"></a>

### 3. Ausführungsgeräte und Infrastruktur
- <a id="k-3-b1"></a>KI bearbeitet Tasks auf physischen Ausführungsgeräten.
- <a id="k-3-b2"></a>Geräte können einer Organization hinzugefügt und Workgroups zugeordnet werden.
- <a id="k-3-b3"></a>Standardmässig stellt DFX eigene Ausführungsserver bereit; Nutzer können optional eigene Hardware, z. B. einen Mac Studio, anbinden.
- <a id="k-3-b4"></a>Initial wird **nur macOS** unterstützt.
- <a id="k-3-b5"></a>Ausführung erfolgt über **Lima-VMs**: pro Workgroup auf einem Ausführungsgerät eine separate VM; bei hoher Last können mehrere VMs pro Workgroup auf mehreren Geräten laufen. Mehrere Tasks derselben Workgroup dürfen dieselbe VM nutzen, müssen aber in **voneinander isolierten KI-Session-Sandboxes** laufen.
- <a id="k-3-b6"></a>Ziel: Beim ersten Task in einer neuen Organization/Workgroup muss die zugehörige VM und Cloud Session in **weniger als zehn Sekunden** bereit sein.
- <a id="k-3-b7"></a>Die Oberfläche zeigt währenddessen einen **Countdown statt eines Spinners**. Bei Verzögerungen muss der tatsächliche Status sichtbar bleiben.
- <a id="k-3-b8"></a>Initiale Serverzuordnung: **[…]** hostet `agent-core`; **[…]** hostet DFX-Cloud-Sessions. *(Servernamen im Repository gekürzt, siehe [Teil D](#d-kuerzungen).)*

<a id="k-4"></a>

### 4. KI-Laufzeit und Modellzugang
- <a id="k-4-b1"></a>In Version 1 wird **Claude Code** als einzige Agentensoftware verwendet.
- <a id="k-4-b2"></a>Nutzer können eigene Claude-Abonnement-Zugangsdaten oder einen Anthropic-API-Key hinterlegen.
- <a id="k-4-b3"></a>Bestehende SMTP-E-Mail-Login-Automatisierung aus `DFXswiss/agent` soll für Claude-Authentifizierung wiederverwendet werden.
- <a id="k-4-b4"></a>Ohne eigene Zugangsdaten und ohne eigene Hardware verwendet das System DFX-Server sowie DFX-Modellzugang über **OpenRouter**; initiales Default-Modell: **DeepSeek**.
- <a id="k-4-b5"></a>Die Kompatibilität von Claude Code mit dem gewählten DeepSeek-Modell über OpenRouter ist technisch zu prüfen.
- <a id="k-4-b6"></a>Spätere Unterstützung anderer Agentensoftware ist optional.

<a id="k-5"></a>

### 5. Verschlüsselung und Vertrauensgrenzen
- <a id="k-5-b1"></a>Vertrauliche Daten, insbesondere Prompts, Antworten, Whiteboards und Zugangsdaten, werden vor der Übertragung an `agent-core` clientseitig verschlüsselt.
- <a id="k-5-b2"></a>**`agent-core` darf vertrauliche Inhalte nicht im Klartext sehen.** Er verwaltet nur notwendige Metadaten und verschlüsselte Inhalte.
- <a id="k-5-b3"></a>Entschlüsselung erfolgt nur bei autorisierten Nutzern und auf autorisierten Ausführungsgeräten.
- <a id="k-5-b4"></a>Bei Nutzung eines DFX-Ausführungsservers **vertraut der Nutzer diesem Server** die Klartextverarbeitung an; Zero Knowledge ist gegenüber dem zentralen Hub gefordert, nicht gegenüber der gewählten Ausführungsumgebung.
- <a id="k-5-b5"></a>Passkey-Login allein liefert nicht automatisch einen Verschlüsselungsschlüssel. Schlüsselableitung, Freigabe und Geräteverteilung sind noch technisch zu definieren.

<a id="k-6"></a>

### 6. Live-Synchronisierung, Transparenz und Audit
- <a id="k-6-b1"></a>Alle relevanten Ereignisse werden **sofort lokal** auf dem Ausführungsgerät in der Datenbank gespeichert und mit möglichst geringer Verzögerung **verschlüsselt zu `agent-core` synchronisiert**.
- <a id="k-6-b2"></a>Dazu zählen Prompts, Antworten, Tool-Ereignisse, Ergebnisse, Status, Fehler, Dateiänderungen, Tokenverbrauch, Kosten, Context-Window-Auslastung, Claude-Abonnements und verfügbare Kontingente, soweit technisch auslesbar.
- <a id="k-6-b3"></a>`agent.dfx.swiss` muss auch auf dem Smartphone den aktuellen Zustand und zeitlich präzise Ereignisse anzeigen.
- <a id="k-6-b4"></a>Jede Aktion, Änderung und Ausführung wird nachvollziehbar protokolliert: Akteur, Zeitpunkt, Task, Session, Workgroup, Gerät, Ziel, Ergebnis und Änderungen.
- <a id="k-6-b5"></a>Vorgeschlagenes technisches Prinzip: **Append-only Event-Log** mit Wiederholung nach Verbindungsunterbrüchen; absolute verzögerungsfreie Netzwerksynchronisierung ist nicht garantierbar.
- <a id="k-6-b6"></a>Standardansicht: Chat mit Prompts und Ergebnissen.
- <a id="k-6-b7"></a>Zusätzliche **native Claude-Code-Session-Ansicht**: vollständige Live-Terminalausgabe und Historie, ausdrücklich **read-only**. Prompts werden nur über die Standardansicht eingegeben.

<a id="k-7"></a>

### 7. Monitoring
- <a id="k-7-b1"></a>Vollständiges Ressourcenmonitoring auf den Ebenen **physisches Gerät → Lima-VM → KI-Session**.
- <a id="k-7-b2"></a>CPU, RAM, Speicher, Netzwerk, Laufzeit, Erreichbarkeit, Zustand und freie Kapazität müssen sichtbar und historisiert sein.
- <a id="k-7-b3"></a>Vorhandenen Monitoring-Stack aus `DFXswiss/server` wiederverwenden, insbesondere **Grafana** und die bereits bewährten Komponenten.
- <a id="k-7-b4"></a>Monitoring-Systeme dürfen getrennt betrieben werden, sollen aber technologisch konsistent sein.
- <a id="k-7-b5"></a>Relevante Metriken werden auch in `agent.dfx.swiss` angezeigt.
- <a id="k-7-b6"></a>Optionale spätere Erweiterungen: Engpasswarnungen und kapazitätsbasierte Task-Verteilung.

<a id="k-8"></a>

### 8. Berechtigungen
- <a id="k-8-b1"></a>Sowohl auf Organization- als auch auf Workgroup-Ebene können Personen hinzugefügt werden.
- <a id="k-8-b2"></a>Organization: **Mitglied = Lesen**, **Administrator = Schreiben**; Standard für neue Mitglieder: Lesen.
- <a id="k-8-b3"></a>Workgroup: Lesen oder Schreiben; in der **Default-Workgroup** haben alle zugehörigen Mitglieder standardmässig Schreibrechte.
- <a id="k-8-b4"></a>Zugriffsrechte müssen technisch im Backend und in der Oberfläche erzwungen werden.
- <a id="k-8-b5"></a>KI-Sessions dürfen über Plattform-APIs alles lesen und entdecken, wofür sie im Kontext ihrer zugewiesenen Rechte autorisiert sind: Mitglieder, Workgroups, Tasks, Status und Whiteboards. Keine pauschale Einsicht in fremde Workgroups.
- <a id="k-8-b6"></a>Lesezugriffe werden ebenfalls protokolliert.

<a id="k-9"></a>

### 9. GitHub
- <a id="k-9-b1"></a>Nutzer können ihren GitHub-Account verbinden und trennen.
- <a id="k-9-b2"></a>Die Plattform unterstützt autorisierten Repository-Zugriff, Branches, Commits, Pushes, Pull Requests und Kommentare.
- <a id="k-9-b3"></a>Rechte sind auf den verbundenen Account und freigegebene Repositories beschränkt; sämtliche Aktionen werden auditiert.
- <a id="k-9-b4"></a>Empfehlung zur Prüfung: GitHub App mit granularen Berechtigungen.
- <a id="k-9-b5"></a>**Wichtig:** Claude Code darf selbst keine Git-Befehle ausführen. GitHub-Aktionen erfolgen ausschliesslich als kontrollierte Plattform-Operationen.

<a id="k-10"></a>

### 10. Harness, Hooks und striktes Ausführungsverbot
- <a id="k-10-b1"></a>Jede Claude-Code-Session erhält automatisch ein standardisiertes Harness mit Organization-, Workgroup-, Task- und Session-Kontext sowie dokumentierten Plattform-APIs.
- <a id="k-10-b2"></a>Das Harness erklärt ausdrücklich die Verbote und die Pflicht, jede gewünschte Aktion über `agent.dfx.swiss` anzufordern.
- <a id="k-10-b3"></a>**Claude Code darf keinerlei Programme, Skripte, Shell-Befehle, Git-Befehle, Tests oder Builds selbst ausführen und keine Subagenten direkt starten.** Subagenten, Tests und Browser werden ausschliesslich als kontrollierte Plattformoperationen beantragt.
- <a id="k-10-b4"></a>Verboten ist auch die Manipulation oder Umgehung von Hooks und Sicherheitsregeln.
- <a id="k-10-b5"></a>Hooks blockieren unerlaubte Aktionen vor Ausführung; die Sandbox muss die Verbote zusätzlich technisch erzwingen.
- <a id="k-10-b6"></a>Die Plattform stellt nur explizit definierte, autorisierte Aktionen bereit, **keine beliebige Shell-Ausführung**.
- <a id="k-10-b7"></a>Die Plattform prüft Aktionen **deterministisch und formell, nicht inhaltlich**. Ausführung erfolgt durch kontrollierte Plattformkomponenten.
- <a id="k-10-b8"></a>Blockierte Versuche werden protokolliert.

<a id="k-11"></a>

### 11. Operationsverwaltung und Duplikatschutz
- <a id="k-11-b1"></a>**Jede** von einer KI-Session angeforderte Operation erhält einen eigenen Datenbankeintrag mit eindeutiger ID, Task-/Session-Bezug, Typ, Parametern, Zeitstempel, Status und Ergebnis.
- <a id="k-11-b2"></a>Die Plattform validiert Operationen formell, prüft Berechtigungen und erkennt offensichtliche Duplikate über deterministische Regeln.
- <a id="k-11-b3"></a>Beispiel: Zweimal dieselbe Kommentar-Anforderung für denselben Pull Request soll nicht unbeabsichtigt zweimal veröffentlicht werden.
- <a id="k-11-b4"></a>Optionaler Mechanismus: Idempotency-Key; Wiederholungsregeln werden je Operationstyp festgelegt.
- <a id="k-11-b5"></a>Auch abgelehnte und deduplizierte Anfragen werden auditiert.
- <a id="k-11-b6"></a>Pull-Request-Erstellung: Session fordert `create_pull_request` an; Plattform prüft formell und erstellt den PR mit dem verbundenen GitHub-Account, dann meldet sie ID und URL zurück.
- <a id="k-11-b7"></a>Ein detailliertes relationales Datenmodell für Pull Requests und ihre Verknüpfungen wurde **bewusst vertagt**.

<a id="k-12"></a>

### 12. Whiteboards und Benachrichtigungen
- <a id="k-12-b1"></a>Je ein Whiteboard auf **Organization-, Workgroup- und Task-Ebene**. **Keine Projektebene.**
- <a id="k-12-b2"></a>Nutzer mit Schreibrechten dürfen Einträge bearbeiten; Nutzer mit Leserechten dürfen sie ansehen.
- <a id="k-12-b3"></a>Änderungen werden verschlüsselt, sofort synchronisiert und mit vollständiger Historie protokolliert.
- <a id="k-12-b4"></a>Beim Erstellen eines Eintrags kann der Autor optional eine Benachrichtigung an die berechtigten Beteiligten der jeweiligen Ebene auslösen.
- <a id="k-12-b5"></a>Aktive Cloud Sessions erhalten nur einen **inhaltsfreien Ping**: Ein neuer Eintrag ist verfügbar.
- <a id="k-12-b6"></a>Die Session entscheidet selbst, ob sie den Eintrag über die Plattform-API abruft; kein automatisches Unterbrechen oder Übermitteln des Inhalts.

<a id="k-13"></a>

### 13. Offene technische Entscheidungen
1. <a id="k-13-1"></a>Konkretes Passkey-gestütztes Schlüsselmanagement und sichere Verteilung der Entschlüsselungsrechte.
2. <a id="k-13-2"></a>Isolation einzelner KI-Sessions innerhalb derselben Lima-VM und Durchsetzung des vollständigen Ausführungsverbots.
3. <a id="k-13-3"></a>Realisierbarkeit des VM-Startziels unter zehn Sekunden.
4. <a id="k-13-4"></a>Claude-Code-/OpenRouter-/DeepSeek-Kompatibilität und technische Verfügbarkeit von Abo-/Kontingentdaten.
5. <a id="k-13-5"></a>Präzise API-Verträge, formelle Validierungsregeln und Idempotenzfenster für Operationsarten.
6. <a id="k-13-6"></a>Details der Gerätezuweisung, Ressourcenplanung und Offline-/Reconnect-Synchronisierung.
7. <a id="k-13-7"></a>Später: detaillierte Pull-Request-Tabellen und Relationen.

<a id="k-14"></a>

### 14. Repositories
- <a id="k-14-b1"></a>https://github.com/DFXswiss/agent
- <a id="k-14-b2"></a>https://github.com/DFXswiss/agent-core
- <a id="k-14-b3"></a>https://github.com/DFXswiss/server

---
Diese Datei dokumentiert die im Gespräch festgelegten Anforderungen. Technische Empfehlungen und offene Punkte sind als solche gekennzeichnet; es handelt sich noch nicht um eine verifizierte Implementierungsspezifikation.

<a id="k-15"></a>

### 15. Erweiterungen 26–47 (verbindliche Produktanforderungen)

<a id="k-idee-26"></a>

#### 26. Kontrollierte Subagenten
Hauptagenten dürfen Subagenten über die Operations-API beantragen, aber nicht direkt starten. Die Plattform startet eine vollwertige Claude-Code-Session in einer separaten Sandbox mit eigenem Harness, Hooks, Logging, Monitoring und Live-Einsicht. Der Subagent berichtet an seinen übergeordneten Agenten und erhält eingeschränkte Rechte.

<a id="k-idee-27"></a>

#### 27. Temporäre Test-VMs
Lint, Prettier, Unit- und Integrationstests werden ausschliesslich über die Plattform beantragt. Jeder Testlauf erhält eine isolierte temporäre Lima-VM. Ergebnisse, Logs und Exit-Codes werden gespeichert; die VM wird danach auch bei Fehlern abgebaut. Claude Code führt keine Tests selbst aus.

<a id="k-idee-28"></a>

#### 28. Temporäre Browser-VMs
Browsernutzung wird über die Plattform beantragt. Sie stellt eine isolierte Ubuntu-Lima-VM mit Browser bereit, den die Session über kontrollierte Schnittstellen steuert. Aktivitäten sind nachvollziehbar und live einsehbar. Nach Gebrauch wird die VM abgebaut.

<a id="k-idee-29"></a>

#### 29. Explizite DFX-Support-Freigabe
Nutzer können eine Workgroup dem DFX-Support mit Lese- oder Schreibrechten freigeben und die Freigabe widerrufen. Ohne Freigabe hat Support keinen Zugriff. Kryptografische Berechtigungen und Audit-Logging gelten auch hier.

<a id="k-idee-30"></a>

#### 30. Encryption by Default ohne UX-Nachteile
Die Sicherheit soll durch standardmässige Verschlüsselung verbessert werden, **ohne das Benutzererlebnis zu verschlechtern**. Passkeys sind die einzige vom Nutzer sichtbare Freigabemethode; Schlüsselmanagement bleibt unsichtbar. Organization-Administratoren erhalten automatisch Leserechte auf alle Workgroups. Rechtevererbung, Schlüsselverteilung und Widerruf benötigen ein Konzept. Zero Knowledge ist ein Ziel, jedoch nicht um den Preis komplizierter Nutzerinteraktionen. Bereits kopierte Klartexte können nicht nachträglich widerrufen werden.

<a id="k-idee-31"></a>

#### 31. Skalierung und Identitätshierarchie
Zielbild: z. B. fünf Mitarbeiter, drei Workgroups und im Durchschnitt hundert parallele Hauptagenten je Mitarbeiter, zuzüglich Subagenten und Test-/Browser-VMs. Menschen, Hauptagenten und Subagenten sind klar unterscheidbar. Hauptagenten haben Namen und GitHub-Username-Zuordnungen (geteilt oder individuell). Subagenten sind untergeordnet, haben geringere Rechte und berichten an den Hauptagenten. Das Dashboard zeigt Verantwortlichkeiten und Beziehungen klar.

<a id="k-idee-32"></a>

#### 32. Autonomous-First
Die Plattform dient primär zur autonomen Ausführung und Orchestrierung grosser Agentenmengen, nicht zum fortlaufenden Chatten. Tasks laufen möglichst ohne menschliche Eingriffe. Das Dashboard priorisiert Agentenstatus, Fortschritt, Blockaden und erforderliche Entscheidungen.

<a id="k-idee-33"></a>

#### 33. KI-gestützte Prozessoptimierung
Ein Dashboard-Button „Prozesse optimieren“ startet eine separate Analyse-Session mit Zugriff auf autorisierte Logs und Metriken. Sie bewertet Effizienz, Tokenverbrauch, Wiederholungen, Subagenten, Workflows und Ressourcen und liefert Verbesserungsvorschläge. Keine automatischen Systemänderungen.

<a id="k-idee-34"></a>

#### 34. Ein delegierter Hauptorchestrator pro Nutzer
Ein Nutzer darf **genau einer** aktiven Claude-Code-Session seine gesamten Plattformrechte innerhalb seines eigenen Berechtigungsrahmens delegieren. Passkey-Freigabe, eigene kryptografische Session-/Geräteidentität ähnlich SSH-Schlüsseln, vollständiges Audit und Widerruf sind erforderlich. Der Orchestrator kann auf DFX-Hardware oder lokal laufen. Auch er verwendet nur kontrollierte Plattformoperationen.

<a id="k-idee-35"></a>

#### 35. Flexible Zuweisung eigener Hardware
Eigene Hardware kann exklusiv einer Workgroup oder der gesamten Organization zur Verfügung gestellt werden. Die Zuweisung steuert Rechenkapazität, nicht Datenzugriff. Workgroups behalten getrennte VMs; Ressourcen werden gemeinsam kapazitätsbewusst verwaltet.

<a id="k-idee-36"></a>

#### 36. Gemeinsame Workgroup-Sessions
Alle schreibberechtigten Workgroup-Mitglieder können eine Session weiterführen, auch wenn ihr Ersteller offline ist. Kontext und Historie bleiben erhalten, Eingriffe werden dem jeweiligen Menschen zugeordnet. Vorschlag: geordnete Eingabewarteschlange für gleichzeitige Anweisungen.

<a id="k-idee-37"></a>

#### 37. GitHub-Identitäten für PRs und Commits
Für PR-Erstellung kann ein verbundener GitHub-Account gewählt werden; Defaults auf Organization-/Workgroup-Ebene sind möglich. Für Commits sollen Firmenidentität, individuelle Mitarbeiter oder mehrere Mitwirkende abbildbar sein. Autor, Committer, Signatur und Co-Autoren sind technisch zu unterscheiden. Intern bleibt die tatsächliche Auslösung nachvollziehbar.

<a id="k-idee-38"></a>

#### 38. Automatische und manuelle Ressourcenwahl
Beim Session-Start wählt die Plattform standardmässig anhand von Berechtigungen und freier Kapazität ein geeignetes Gerät und eine Lima-VM. Eine Workgroup kann mehrere VMs auf mehreren Geräten verwenden. Autorisierte Nutzer können Gerät/VM manuell auswählen. Die Auswahl erfolgt deterministisch.

<a id="k-idee-39"></a>

#### 39. Session-Abschluss und Cleanup
Bei bestätigtem Task-Abschluss wird der Claude-Code-Prozess beendet, Ressourcen und Sandbox werden freigegeben. Logs, Ergebnisse und Kontext bleiben erhalten. Eine Lima-VM bleibt bei anderen aktiven Sessions bestehen und kann sonst heruntergefahren werden. Abschluss muss explizit festgestellt werden.

<a id="k-idee-40"></a>

#### 40. Vollständiges Session Resume
Bei späteren Änderungswünschen, etwa zu einem bestehenden PR, identifiziert die Plattform die frühere Task-/Session-Zuordnung und stellt **die ursprüngliche Claude-Code-Session mit ihrem vollständigen Kontext** wieder her. Resume ist auf einer anderen geeigneten Lima-VM möglich; es genügt nicht, nur eine Zusammenfassung als neue Session zu starten.

<a id="k-idee-41"></a>

#### 41. Pull Request Conflict Auto-Fix
Ein Button auf PR-Ebene startet die KI-gestützte Behebung von Merge-Konflikten, vorzugsweise per Resume der ursprünglichen Session. Änderungen, GitHub-Aktionen und optionale Tests laufen über kontrollierte Plattformoperationen. Der bestehende PR-Branch wird aktualisiert; ein automatischer Merge ist nicht Bestandteil dieser Funktion.

<a id="k-idee-42"></a>

#### 42. Spezialisierte Agentenrollen
Konfigurierbare Rollen und API-Berechtigungen erlauben mehrstufige Workflows: Development Agents dürfen etwa Commits/PRs beantragen, aber nicht mergen; Security Agents liefern Scans; Review Agents bewerten Nachweise; Merge Agents prüfen formelle Freigabekriterien und beantragen einen Merge. GitHub-Identitäten können je Session verschieden sein. Arbeit, Prüfung und Freigabe werden getrennt.

<a id="k-idee-43"></a>

#### 43. Ein Hauptagent pro Task, frischer Kontext
**Jeder Task erhält seinen eigenen Hauptagenten mit frischem Claude-Code-Kontext.** Es gibt keine automatische Übernahme der Historie anderer Tasks. Berechtigte Plattformdaten können dennoch abgerufen werden. Die Fortsetzung desselben Tasks nutzt die ursprüngliche Session und deren Kontext.

<a id="k-idee-44"></a>

#### 44. Wiederherstellung nur für Cloud Sessions
Cloud Sessions werden kontinuierlich so synchronisiert, dass sie nach Geräteausfall **mit Unterbrechung**, aber ohne Verlust des zuletzt erfolgreich synchronisierten Kontexts auf einem anderen Gerät fortgesetzt werden können. Temporäre Test- und Ubuntu-Browser-VMs benötigen kein Resume; sie werden bei Ausfall neu gestartet. Ihre Logs/Ergebnisse werden weiterhin gespeichert.

<a id="k-idee-45"></a>

#### 45. Kostenlos bei vollständig eigenen Ressourcen
Wer sowohl eigene Ausführungshardware als auch eigene KI-Zugänge verwendet, bezahlt **keine DFX-Plattformgebühr**. Eigene Hardware-, Strom- und Abo-/API-Kosten bleiben beim Nutzer.

<a id="k-idee-46"></a>

#### 46. Verbrauchsabhängige DFX-Abrechnung
DFX-Modellnutzung wird tokenbasiert, DFX-VM-Laufzeit sekundengenau verrechnet; auch Subagenten, Test- und Browser-VMs zählen. Verbrauch und Kosten sind Tasks, Sessions, Workgroups und Organizations zuordenbar und im Dashboard sichtbar. Gemischte Nutzung wird nur für tatsächlich verwendete DFX-Ressourcen berechnet:
- <a id="k-idee-46-b1"></a>Eigene Hardware + eigener KI-Zugang: keine DFX-Gebühr.
- <a id="k-idee-46-b2"></a>DFX-Hardware + eigener KI-Zugang: VM-Laufzeit.
- <a id="k-idee-46-b3"></a>Eigene Hardware + DFX-KI-Zugang: Tokens.
- <a id="k-idee-46-b4"></a>DFX-Hardware + DFX-KI-Zugang: VM-Laufzeit und Tokens.

<a id="k-idee-47"></a>

#### 47. Prepaid-Guthaben
Kostenpflichtige DFX-Ressourcen werden ausschliesslich aus vorausbezahltem Guthaben finanziert. Token- und VM-Verbrauch reduzieren den Saldo laufend; der Stand ist jederzeit sichtbar. Ohne Guthaben dürfen keine weiteren kostenpflichtigen Ressourcen genutzt werden. Laufende kostenpflichtige Sessions werden kontrolliert gestoppt, bleiben wiederaufnehmbar und können nach Aufladung fortgesetzt werden. Vollständig eigene Ressourcen bleiben unabhängig vom Guthaben nutzbar.

<a id="k-16"></a>

### 16. Abschliessende Konzeptentscheidungen und technische Prüfungen
- <a id="k-16-b1"></a>Das System ist **autonomous-first** und muss auf Hunderte parallele Hauptagenten plus Subagenten und temporäre Umgebungen ausgelegt sein.
- <a id="k-16-b2"></a>Es gibt **nur** Organization → Workgroup → Task, keinen Projekttyp als zusätzliche Hierarchieebene.
- <a id="k-16-b3"></a>Ein Task hat einen Hauptagenten mit frischem Kontext; die zugehörige Session ist nach Abschluss wiederaufnehmbar.
- <a id="k-16-b4"></a>Hauptagenten dürfen weder direkt Programme ausführen noch Subagenten starten; autorisierte Aktionen laufen über die Plattform.
- <a id="k-16-b5"></a>Geräteausfälle dürfen Cloud Sessions unterbrechen, aber nicht die Wiederaufnahme des synchronisierten Kontexts verhindern.
- <a id="k-16-b6"></a>Kostenpflichtig sind nur von DFX bereitgestellte Modell- und Ausführungsressourcen; Zahlung per Prepaid.
- <a id="k-16-b7"></a>Noch technisch zu prüfen: tatsächliche Session-Restore-Fähigkeit über Geräte hinweg; Synchronisierungs- und Schlüsselmodell; Sandbox-Isolation und Hook-Umgehungsschutz; Laufzeit-/Token-Messung und Guthaben-Abbruch; Startzeit unter zehn Sekunden; Claude Code über OpenRouter/DeepSeek; zuverlässige Operations-Idempotenz und GitHub-Identitäten.

---
**Ende der abgeschlossenen Konzeptfassung mit 47 Ideen.** Dieses Dokument beschreibt die vereinbarten Produktanforderungen; es behauptet nicht, dass diese bereits implementiert oder technisch validiert wurden.

## Teil B – Regeln und Entscheide des Auftraggebers

Jünger als das Konzept; bei Widerspruch gilt die jüngere ausdrückliche Aussage. Jede Regel hat eine feste Kennung,
das Datum (Uhrzeit in mitteleuropäischer Zeit, wo bekannt), die Regel in einem Satz, den Wortlaut des Auftraggebers
(Kleinschreibung und Tippfehler wie im Original; „[…]“ markiert eine Kürzung, meist eines internen Namens), den
Bezug zum Konzept und wo sie umgesetzt ist. „Gebaut“ heißt: auf dem Integrationsstand vorhanden; „offen“ heißt:
das genannte Paket setzt sie um (auch in [Teil D](#d-offen) geführt).

<a id="e-01"></a>

### E-01 – Die vollständige Konzeptfassung ist maßgeblich

- **Datum:** 2026-11-23 19:30
- **Regel:** Maßgeblich ist die vollständige Konzeptfassung mit 47 Ideen ([Teil A](#teil-a--das-konzept)), nicht die
  frühere Kurzfassung mit den Ideen 1–26.
- **Wortlaut:** kein Satz überliefert; der Auftraggeber hat die vollständige und die geprüfte Fassung an diesem Abend
  übergeben.
- **Bezug:** ersetzt die Kurzfassung; neu sind [§15](#k-15) (Ideen 26–47) und [§16](#k-16), geändert
  [§3 Punkt 5](#k-3-b5) und [§10 Punkt 3](#k-10-b3).
- **Umsetzung:** Abgleich und Bauplan je Idee in [spec/01-requirements.md](spec/01-requirements.md) (Block R-16).

<a id="e-02"></a>

### E-02 – „Hunderte Agenten“ beschreibt die Nutzung, es ist kein Zahlenziel

- **Datum:** 2026-11-23 19:45
- **Regel:** Die Plattform hat keine künstliche Obergrenze, wächst mit Geräten und Modellzugängen mit und meldet bei
  erreichter Kapazität klar, statt zu scheitern; eine bestimmte Zahl ist nicht verlangt.
- **Wortlaut:** „keine technische anforderung sondern eine aussage die erklärt wie ich es nutzen will, es können auch
  10 sein, oder 1000“
- **Bezug:** präzisiert [Idee 31](#k-idee-31) und [§16 Punkt 1](#k-16-b1).
- **Umsetzung:** Grenzen als Einstellungen ([A5](#a-5), [A14](#a-14); offen – KOR-45); Volumenfestigkeit KOR-35
  (offen).

<a id="e-03"></a>

### E-03 – Der Nutzer legt nur seinen Passkey an, alles andere richtet sein KI-Agent ein

- **Datum:** 2026-11-23
- **Regel:** Jeder Kunde kann alles Weitere vollautomatisch von seinem KI-Agenten einrichten lassen – auch sein
  Claude-Abo durch Übertragen des vorhandenen Auth-Tokens; nichts davon setzt ein Terminal beim Nutzer voraus.
- **Wortlaut:** „das muss aber auch mittels übertragung der auth token ganz unkompliziert möglich sein! zwingend! und
  alles vollautomatisch von dir erledigt, nicht dass ich das selber machen will!!! jeder Kunde von uns muss dass alles
  vollautomatisch von seinem ki agent einrichten lassen können, das einzige was der nutzer noch selber macht ist ein
  passkey anlegen, alles andere macht sein ki agent!“ – dazu: „ich weiss nicht was ein terminal ist“
- **Bezug:** ergänzt [Grundprinzip](#k-gp) (Einstieg ohne Konfiguration), [§4 Punkt 2](#k-4-b2) und
  [Idee 34](#k-idee-34).
- **Umsetzung:** gebaut – [ADR-056](adr/056-delegated-agent-access.md) (KOR-24), Anleitung [agents.md](agents.md).

<a id="e-04"></a>

### E-04 – Gleichstellung des Agentenzugangs

- **Datum:** 2026-11-23 17:40
- **Regel:** Ein KI-Agent, der ohne Passkey und ohne Browser verbunden ist, kann alles tun, was ein Mensch mit Passkey
  in der Oberfläche tun kann; jede neue Fähigkeit der Oberfläche ist auch über die Schnittstellen bedienbar.
- **Wortlaut:** „ganz wichtig: auch ein ki agent der sich nur mit ssh verbindet (oder wie du das nennen willst, also
  ich meine die "nicht passkey" variante, soll alles tun können was ein mensch der sich mit passkey verbindet tun kann.
  zwingend!“
- **Bezug:** ergänzt [§8](#k-8) und [Idee 34](#k-idee-34).
- **Umsetzung:** gebaut für den Bestand – [ADR-056](adr/056-delegated-agent-access.md) (KOR-24),
  [agent-parity.md](agent-parity.md); gilt für jede neue Fähigkeit.

<a id="e-05"></a>

### E-05 – Fehlende Voraussetzungen werden angestoßen, nicht erklärt

- **Datum:** 2026-11-23
- **Regel:** Fehlt für eine Aufgabe eine Verbindung oder Voraussetzung, erkennt die Plattform das sofort und stößt
  den fehlenden Schritt an (zum Beispiel die GitHub-Anmeldung), statt zu erklären, was fehlt.
- **Wortlaut:** „in so einem fall muss direkt erkannt werden dass ein GitHub Login benötigt wird und die einzige
  korrekte Antwort besteht darin das GitHub Login anzustossen“
- **Bezug:** ergänzt [§9](#k-9) und [Idee 32](#k-idee-32); gilt als Muster für jede fehlende Voraussetzung.
- **Umsetzung:** gebaut – für GitHub [ADR-066](adr/066-held-github-operations.md) (KOR-25); für Modellzugang,
  Guthaben, Gerät und Plattformzugang [ADR-079](adr/079-no-approval-as-precondition.md) (KOR-49); jede Voraussetzung
  mit dem Schritt, den die Plattform anstößt, im Register [spec/15-prerequisites.md](spec/15-prerequisites.md).

<a id="e-06"></a>

### E-06 – GitHub über das Konto des Betreibers für freigegebene Nutzer

- **Datum:** 2026-11-23
- **Regel:** Freigegebene Nutzer arbeiten sofort mit dem GitHub-Konto des Betreibers; der Auftraggeber richtet bei
  GitHub nichts selbst ein.
- **Wortlaut:** „ich will dass es mit meinem […] GitHub Account funktioniert“ (Kontoname gekürzt)
- **Bezug:** ergänzt [§9 Punkt 1](#k-9-b1) und [Idee 37](#k-idee-37).
- **Umsetzung:** gebaut – [ADR-053](adr/053-operator-github-account.md) (KOR-27),
  [ADR-054](adr/054-repository-list-optional.md) (KOR-30); allgemeiner Verbindungsweg
  [ADR-065](adr/065-github-existing-token.md) (KOR-25); das Betreiberkonto als Konto seiner Organization und mehrere
  Konten mit Wahl je Operation, Session und Aufgabe [ADR-078](adr/078-github-identities.md) (KOR-47).

<a id="e-07"></a>

### E-07 – Betreiber-Abos für freigegebene Organisationen

- **Datum:** 2026-11-23
- **Regel:** Freigegebene Organisationen nutzen die Claude-Abos des Betreibers auf dem Plattform-Gerät, ohne selbst
  etwas zu verbinden.
- **Wortlaut:** „auch die abos verbinden ich will nicht selber machen, du hast doch alle abos, warum hast du sie
  nicht hinzugefügt?“
- **Bezug:** ergänzt [§4 Punkt 2](#k-4-b2) und [§4 Punkt 4](#k-4-b4); zur Abrechnung [Idee 46](#k-idee-46).
- **Umsetzung:** gebaut – [ADR-052](adr/052-operator-subscriptions.md) (KOR-23); ihr Verbrauch wird berechnet –
  [ADR-062](adr/062-prepaid-credit-and-usage-billing.md) (KOR-31).

<a id="e-08"></a>

### E-08 – Arbeits-VM auf Anforderung, in der alles erlaubt ist

- **Datum:** 2026-11-23 18:35
- **Regel:** Eine Session fordert über eine kontrollierte Plattform-Operation eine eigene, separate, isolierte
  Ubuntu-VM an und kann darin alles (root, installieren, Programme, Tests, Builds, Internet, Browser); die VM ist nach
  außen dicht und enthält keine Zugangsdaten der Plattform (GitHub bleibt bei den kontrollierten
  Plattform-Operationen), und eine Session sagt nie, sie könne etwas nicht ausführen.
- **Wortlaut:** „ich will dass meine ki sessions alles tun können, die sollen vollständig produktiv sein! zwingend!
  die Session selbst ist nur eine session, aber wenn ein ki agent sowas wie eine software ausführen will oder so muss
  der ki agent eine ubuntu vm anfordern können, dann bekommt er zugriff auf eine separate isolierte ubuntu vm in einem
  eigenen lim vm und er kannt damit alles machen, ich meine wirklich alles, keine einschränkungen ,ich will hier
  maximale produktivität!“
- **Bezug:** ersetzt für die Arbeits-VM das Ausführungsverbot aus [§10 Punkt 3](#k-10-b3), [§10 Punkt 6](#k-10-b6)
  und [§16 Punkt 4](#k-16-b4) (in der Session selbst wird weiterhin nichts ausgeführt); ergänzt
  [Idee 27](#k-idee-27) und [Idee 28](#k-idee-28) ([A6](#a-6)).
- **Umsetzung:** gebaut – [ADR-059](adr/059-work-vm-on-request.md) (KOR-29).

<a id="e-09"></a>

### E-09 – In der Arbeits-VM wird nichts überwacht

- **Datum:** 2026-11-23 18:52
- **Regel:** Was in der Arbeits-VM geschieht, wird weder in der VM noch auf dem Gerät überwacht; legt der Agent dort
  selbst Zugangsdaten ab, ist das seine Sache (die Plattform legt keine hinein), und geschützt wird nur die
  Außengrenze (Gerät, andere VMs und internes Netz sind aus der VM unerreichbar).
- **Wortlaut:** „wenn der agent sich entscheidet in der vm selbst einen zugangstoken für irgendwas hinzuzufügen, dann
  ists halt so. ist nicht die idee der sache aber ich sehe es nicht als meine aufgabe seine aktivitäten in der vm zu
  überwachen, […]“ (Fortsetzung zum Ausgang siehe E-10)
- **Bezug:** wie E-08; gegenüber [§6 Punkt 4](#k-6-b4) und [§10 Punkt 8](#k-10-b8) gilt: protokolliert werden die
  Plattform-Operationen (Anfordern, Abbau, Ergebnisse), nicht die Vorgänge in der VM.
- **Umsetzung:** gebaut – [ADR-059](adr/059-work-vm-on-request.md) (KOR-29).

<a id="e-10"></a>

### E-10 – Der Ausgang der Arbeits-VMs läuft nie über die Standortadresse

- **Datum:** 2026-11-23 18:52, ergänzt 2026-11-25 11:12
- **Regel:** Arbeits-VMs erreichen das Internet nur über geteilte Ausgangsadressen eines Dienstleisters, nie über die
  Adresse des Standorts, an dem das Gerät steht; fällt dieser Weg aus, hat die VM kein Internet (fail-closed). Eine
  feste Ausgangsadresse ist nicht verlangt. Bei Cloudflare werden nur die kostenlosen Tarife benutzt; eine eigene
  feste Ausgangsadresse (kostenpflichtiger Zusatz) wird nicht beschafft, auch kein Angebot dafür eingeholt
  (Ergänzung vom 2026-11-25).
- **Wortlaut:** „[…] allerdings sollten wir schauen dass er nicht mit meiner ip ins internet geht […]“ (18:52; die
  dort verlangte eigene statische Adresse hat er um 20:40 zurückgenommen:) „wir brauchen keine fixe ip für ubuntu vm,
  es geht nur darum dass sie nicht die normale ip von […] standort nimmt“ (Standortname gekürzt). Am 2026-11-25
  11:12 auf die Frage, ob ein Angebot für eine eigene feste Ausgangsadresse eingeholt werden soll: „nein, wir bleiben
  bei den kostenlosen modellen“ („Modelle“ als Tarife ausgelegt).
- **Bezug:** ergänzt E-08 und [§3](#k-3); die Ergänzung bestätigt die Rücknahme vom 2026-11-23 20:40 und schließt
  kostenpflichtige Cloudflare-Tarife auch für andere Zwecke aus.
- **Umsetzung:** gebaut – [ADR-059](adr/059-work-vm-on-request.md) (KOR-29; geteilte Adressen des Cloudflare-Clients
  ohne kostenpflichtigen Tarif); die frühere Folgerung „eigene statische Adresse“ ist dort als nicht verfolgt
  gekennzeichnet (DOC-05).

<a id="e-11"></a>

### E-11 – Keine unnötigen Einschränkungen, nichts einzeln freigeben müssen

- **Datum:** 2026-11-23 19:52
- **Regel:** Was der Nutzer oder sein verbundener Account darf, darf auch seine KI, ohne dass vorher etwas
  eingetragen, freigeschaltet oder bestätigt wird; einschränkende Einstellungen sind freiwillige Optionen, nie
  Voraussetzung, und jede Stelle, an der eine Aufgabe an einer fehlenden Freigabe scheitert, ist ein Fehler – außer
  sie ist technisch oder zum Schutz Dritter zwingend (dann gilt E-05).
- **Wortlaut:** „ich will keine sachen spezifisch freigeben müssen, der ki agent soll maximal produktiv sein, jede
  unnötige einschränkung soll nicht existieren“
- **Bezug:** geht [Idee 26](#k-idee-26) („eingeschränkte Rechte“), [Idee 31](#k-idee-31) („geringere Rechte“),
  [Idee 42](#k-idee-42) und [§9 Punkt 3](#k-9-b3) („freigegebene Repositories“) vor ([A1](#a-1), [A5](#a-5)).
- **Umsetzung:** gebaut für Repositories – [ADR-054](adr/054-repository-list-optional.md) (KOR-30) – und für die
  Arbeits-VM ohne Befehlsfilter – [ADR-059](adr/059-work-vm-on-request.md) (KOR-29); Inventur, Register mit Prüfung,
  vom Root signierte Schalter und die Betreiber-Abo-Liste im Plattform-Log, keine geschützten Branches als
  Voreinstellung – [ADR-079](adr/079-no-approval-as-precondition.md) (KOR-49); die Schalter legt der Betreiber nach
  dem Entscheid des Auftraggebers um (Teil D).

<a id="e-12"></a>

### E-12 – Registrierung nur mit einem PRF-fähigen Passkey

- **Datum:** 2026-11-23
- **Regel:** Ein Passkey ohne PRF wird schon bei der Registrierung mit einem klaren Hinweis abgewiesen.
- **Wortlaut:** „wenn wir PRF benötigen und jemand ein Passkey verwendet der kein RPF unterstützt dann muss es schon
  beim registrieren scheitern mit klarem Hinweis: Du brauchst einen PAsskey der PRF unterstützt. Hinweis: Bitwarden
  unterstützt kein PRF.“
- **Bezug:** präzisiert [§1 Punkt 3](#k-1-b3) und [Idee 30](#k-idee-30).
- **Umsetzung:** gebaut – [ADR-055](adr/055-registration-without-recovery-code-step.md) (KOR-22).

<a id="e-13"></a>

### E-13 – Kein Pflichtschritt „Wiederherstellungscode“ in der Registrierung

- **Datum:** 2026-11-23
- **Regel:** Die Registrierung endet nach dem Passkey; einen Wiederherstellungscode erstellt der Nutzer freiwillig in
  den Einstellungen.
- **Wortlaut:** „das gibt ein schlechtes benutzererlebnis, das muss weg“ (zum Bildschirm „Ihr Wiederherstellungscode“)
- **Bezug:** folgt aus [Idee 30](#k-idee-30) (keine UX-Nachteile, Passkey als einzige sichtbare Freigabe).
- **Umsetzung:** gebaut – [ADR-055](adr/055-registration-without-recovery-code-step.md) (KOR-22).

<a id="e-14"></a>

### E-14 – Abrechnung nur über Guthaben, das der Betreiber setzt

- **Datum:** 2026-11-23 20:00, ergänzt 2026-11-25 11:12
- **Regel:** Kostenpflichtige Ressourcen verbrauchen nur Guthaben; es gibt keinen Zahlungsweg und kein Aufladen in der
  Oberfläche, das Guthaben setzt der Betreiber (direkt in der Datenbank, gleichwertig ein Befehl mit Audit). Guthaben
  entsteht nur auf zwei Wegen: gekauft oder vom Betreiber manuell verteilt; ein automatisches Startguthaben gibt es
  nicht, auch nicht für neue Organisationen (Ergänzung vom 2026-11-25). Der Kauf ist heute nicht gebaut (kein
  Zahlungsweg, siehe oben). Verteilt wird Guthaben durch einen direkten Eingriff des Betreibers auf dem Hub-Gerät; ein
  öffentlicher Endpunkt oder eine Ansicht zum Verteilen wird nicht gebaut (Ergänzung vom 2026-11-25).
- **Wortlaut:** „zahlungswege gibt es keinen, man kann nur guthaben verbrauchen, guthaben aufladen aktuell nicht
  möglich. guthaben setze ich nach eigenem gutdünken manuell in der db über […]. wir starten einfach mal so.“
  (interner Zugangsweg gekürzt). Am 2026-11-25 11:12 auf die Frage nach 20 USD Startguthaben je neuer
  Organisation: „es gibt kein automatisches startguthaben, guthaben wird nur entweder gekauft oder manuell von mir
  verteilt. das ist alles“; am 2026-11-25 vormittags: „das verteilen mache ich mit direktem remote ssh eingriff auf
  […], kein öffentlicher api endpoint notwneid.g“ (Gerätename gekürzt; gemeint ist das Hub-Gerät, „notwneid.g“ =
  notwendig)
- **Bezug:** präzisiert [Idee 47](#k-idee-47) („nach Aufladung“ = nach einer Gutschrift des Betreibers) und
  [Idee 46](#k-idee-46); die Ergänzung beantwortet die offene Frage F1 aus
  [ADR-079](adr/079-no-approval-as-precondition.md) (Startguthaben statt Freigabe): kein Startguthaben.
- **Umsetzung:** gebaut – [ADR-062](adr/062-prepaid-credit-and-usage-billing.md) (KOR-31); die Einstellung
  Startguthaben bleibt 0 (Voreinstellung), vor dem Einschalten der Abrechnung verteilt der Betreiber Guthaben an die
  bestehenden Organisationen ([Runbook](runbooks/credit.md), DOC-05).

<a id="e-15"></a>

### E-15 – Preise als fairer Marktdurchschnitt

- **Datum:** 2026-11-23 20:00
- **Regel:** Die Preise werden nach belegter Marktrecherche als fairer Durchschnitt vergleichbarer Anbieter
  festgelegt und stehen in der Konfiguration, nicht im Code.
- **Wortlaut:** „preise kannst du selber entscheiden, ermittle den marktpreis und orientiere dich an anderen
  anbietern. nimm einen fairen durchschnittspreis.“
- **Bezug:** ergänzt [Idee 46](#k-idee-46).
- **Umsetzung:** gebaut – [ADR-062](adr/062-prepaid-credit-and-usage-billing.md) (KOR-31).

<a id="e-16"></a>

### E-16 – Alles wird in US-Dollar bepreist

- **Datum:** 2026-11-23 23:05
- **Regel:** Preisliste, Buchungen, Guthaben, Limits, Anzeigen, Schnittstellen, Betreiber-Befehle und Berichte sind
  immer in USD; es gibt keine zweite Anzeigewährung und keine Umrechnung in der Anzeige (zum Buchhaltungsfeld in CHF
  siehe E-17).
- **Wortlaut:** „es kann sein dass es im konzept anderst definiert ist, ich habe mich aber jetzt dazu entschieden
  alles in agent immer in usd zu bepreisen“
- **Bezug:** geht jeder abweichenden Angabe in [Idee 45](#k-idee-45)–[47](#k-idee-47) und in älteren Texten vor.
- **Umsetzung:** gebaut – [ADR-062](adr/062-prepaid-credit-and-usage-billing.md) (KOR-31; Beträge als Mikro-USD).

<a id="e-17"></a>

### E-17 – Jede Buchung trägt zusätzlich ihren CHF-Gegenwert über einen zentralen Tageskurs

- **Datum:** 2026-11-23 23:15
- **Regel:** Jede Buchung speichert unveränderlich ihren Gegenwert in CHF zusammen mit dem verwendeten
  USD/CHF-Kurs und dessen Datum; der Kurs liegt zentral in einer Tabelle (ein Kurs je Tag, mit Quelle und Zeitpunkt),
  wird außerhalb des Hubs gepflegt (Betreiber-Befehl; ein täglicher Betriebsjob holt ihn aus einer amtlichen Quelle)
  und nicht je Buchung abgefragt. Bepreist, geführt und angezeigt wird weiter in USD.
- **Wortlaut:** „aufgrund der buchhaltung sind wir verpflichtet jede buchung immer in CHF gegenwert zu speichern,
  dafür reicht es aber einen USD / CHF Tageskurs einmal zentral zu definieren, bzw in einer db abzulegen der dann
  immer benutzt wird“
- **Bezug:** ergänzt E-16 und [Idee 46](#k-idee-46).
- **Umsetzung:** gebaut – [ADR-062](adr/062-prepaid-credit-and-usage-billing.md) (KOR-31).

<a id="e-18"></a>

### E-18 – Bestehende Konten werden direkt in eine Organization aufgenommen

- **Datum:** 2026-11-23 22:10
- **Regel:** Wer ein Konto hat, wird vom Administrator oder dessen Agenten direkt in eine Organization aufgenommen,
  ohne Zutun des Aufgenommenen; eine Einladung, die jemand selbst annehmen muss, bleibt nur für Menschen ohne Konto.
- **Wortlaut:** „die 3 personen haben ihren einladungslink angenommen und für alles andere bist jetzt du zuständig,
  das muss reichen!“ – früher: „alles muss von dir gemacht werden können, zwingend!“
- **Bezug:** ergänzt [§1 Punkt 1](#k-1-b1) und [§8 Punkt 1](#k-8-b1): Die Einladung bleibt der einzige Weg auf die
  Plattform, aber nicht der einzige Weg in eine weitere Organization.
- **Umsetzung:** gebaut – [ADR-060](adr/060-direct-admission-of-existing-accounts.md) (KOR-37, direkte Aufnahme);
  Einladung mit einem Klick gebaut (KOR-36); Namen der Aufgenommenen ohne ihr Zutun –
  [ADR-073](adr/073-names-for-every-member.md) (HOT-12).

<a id="e-19"></a>

### E-19 – Keine Lösung nur für den Einzelfall: Fehlerklassen ausschließen

- **Datum:** 2026-11-23
- **Regel:** Ein Fehler wird nicht nur an der gemeldeten Stelle behoben, sondern die ganze Fehlerklasse wird
  ausgeschlossen und mit einer dauerhaften Prüfung belegt, sodass er nicht wieder auftreten kann.
- **Wortlaut:** „ich bin da nicht so begeistert von wenn du hier eine lösung umsetzt die nur für diesen fall
  funktioniert, sowas darf nicht passieren können“
- **Bezug:** gilt für jede Korrektur; erster Fall: Datenmenge legt eine Ansicht lahm ([§6 Punkt 3](#k-6-b3),
  [Idee 31](#k-idee-31), [Idee 32](#k-idee-32)).
- **Umsetzung:** teils – Grundsatz für alle Pakete; erster Fall KOR-35 (offen).

<a id="e-20"></a>

### E-20 – Community-Bereich mit Gruppenchat aller Nutzer

- **Datum:** 2026-11-23
- **Regel:** agent.dfx.swiss hat einen Bereich „Community“ mit einem Gruppenchat aller Nutzer.
- **Wortlaut:** „ich hätte gern auf agent.dfx.swiss einen "Community" Bereich, dort soll es einen Chat geben wo man
  als eine Art Gruppenchat mit allen Nutzern von agent.dfx.swiss chatten kann.“
- **Bezug:** neu; das Konzept kennt keinen Bereich über Organizations hinweg ([§12](#k-12) gilt je Ebene).
- **Umsetzung:** offen – KOR-26.

<a id="e-21"></a>

### E-21 – Aufgabenübersicht als Menüpunkt

- **Datum:** 2026-11-23
- **Regel:** Die Aufgaben aller lesbaren Workgroups sind über einen eigenen Menüpunkt erreichbar.
- **Wortlaut:** „ich vermusse auf agent.dfx.swiss die task übersicht, wo ist die?“
- **Bezug:** ergänzt [Idee 32](#k-idee-32) und [Idee 31](#k-idee-31) (Übersicht über Tasks hinweg).
- **Umsetzung:** gebaut – KOR-32 (Menüpunkt „Tasks“).

<a id="e-22"></a>

### E-22 – Ein Repository

- **Datum:** 2026-11-23 22:05
- **Regel:** Die Repositories werden zusammengelegt; übrig bleibt nur `DFXswiss/agent`.
- **Wortlaut:** „repos zusammenlegen, nur noch /agent“
- **Bezug:** ersetzt [§14](#k-14) für die Zeit nach der Zusammenlegung (`DFXswiss/agent-core` wird archiviert; der
  Name `agent-core` bleibt für den Hub als Komponente, [Grundprinzip](#k-gp)).
- **Umsetzung:** gebaut – [ADR-061](adr/061-one-repository.md) (INF-06).

<a id="e-23"></a>

### E-23 – Konzept und Regeln stehen im Repository, in einer zentralen Datei

- **Datum:** 2026-11-23 23:25
- **Regel:** Konzept und verbindliche Regeln stehen in dieser Datei im Repository; jede neue Vorgabe kommt zuerst
  hierher ([Teil D](#teil-d--pflege)).
- **Wortlaut:** „solche sachen müssen im repo als regeln definiert sein, in einer zentralen md datei welche das
  konzept beschreibt“
- **Bezug:** neu.
- **Umsetzung:** gebaut – KOR-38 (diese Datei, Prüfung `tests/platform/test_konzept.py`).

<a id="e-24"></a>

### E-24 – Kein vorgeschalteter Zugangsschutz: die Plattform schützt sich selbst

- **Datum:** 2026-11-23 16:40
- **Regel:** Vor agent.dfx.swiss steht keine zusätzliche Zugangsschicht eines Dienstleisters; die Plattform ist
  öffentlich erreichbar und schützt sich selbst (Einladungscode, Passkey, signierte Anfragen, Ratenbegrenzung), und
  eine solche Schicht wird nicht wieder vorgeschlagen.
- **Wortlaut:** „den cloudeflar schutz für agent.dfx.swiss habe ich entfernt, den will ich nicht“
- **Bezug:** ergänzt [§1](#k-1) und [§5](#k-5).
- **Umsetzung:** gebaut (Betrieb).

<a id="e-25"></a>

### E-25 – Keine unbestellten Schutzschichten: Nutzbarkeit, Tempo und Leistung zuerst

- **Datum:** 2026-11-24 09:35
- **Regel:** Schutzschichten, Härtungen und Umbauten, die über das Bestellte hinausgehen und Entwicklungszeit oder
  Leistung kosten, werden erst gebaut, wenn der Auftraggeber sie entschieden hat; bis dahin haben die produktive
  Nutzbarkeit, die Fertigstellung und die Leistung der Plattform Vorrang.
- **Wortlaut:** „das scheint mir übertrieben“ (zu einer zusätzlichen Abschottung der Arbeits-VMs) · „das scheint mir
  auch übertrieben zu sein“ (zu einer zweiten Schutzschicht um laufende Sessions) · „ich möchte agent.dfx.swiss so
  schnell wie möglich produktiv nutzen können, nicht schutzschichten bauen die ich nie bestellt habe“ ·
  „Schutzschicht ist schon etwas gutes aber es kostet uns jetzt wertvolle Zeit und nachher Performance, du kannst
  nicht einfach ohne Rückfrage mit mir solche entscheidungen treffen, ich will mehr Performance und schneller fertig
  werden!“
- **Bezug:** ergänzt [E-24](#e-24) und die Arbeits-VM ([E-08](#e-08)) sowie die isolierten Sessions
  ([§3 Punkt 5](#k-3-b5)).
- **Umsetzung:** gilt für jedes Paket. Zwei geplante zusätzliche Abschottungen (Arbeits-VMs in einer eigenen
  Umgebung; zweite Netzgrenze im Gast der Sessions) sind zurückgestellt, bis der Auftraggeber sie bestellt.

<a id="e-26"></a>

### E-26 – KI-Agenten sind Nutzer wie Menschen: die Bedienung durch sie ist einfach und effizient

- **Datum:** 2026-11-24 09:40
- **Regel:** Die Plattform wird nicht nur von Menschen, sondern auch von KI-Agenten benutzt; die Bedienung durch
  KI-Agenten ist einfach (ein Agent findet ohne Vorwissen selbst heraus, wie er etwas tut) und effizient (er liest
  und sendet nur, was der nächste Schritt braucht).
- **Wortlaut:** „agent.dfx.swiss soll nicht nur von Menschen benutzt werden können sondern auch von ki agents. was
  müssen wir tun damit die bedienung durch ki agents einfach und effizient ist?“
- **Bezug:** ergänzt [E-04](#e-04) und [Idee 34](#k-idee-34).
- **Umsetzung:** gebaut – `agent-platform guide` (kurzer Einstieg), `search`, `guide --topic`, `--limit`/`--fields`
  und der lokale MCP-Server `mcp` mit `install-connector` (KOR-63, [Entscheidungen](decisions/KOR-63.md)),
  Anleitung [agents.md](agents.md).

<a id="e-27"></a>

### E-27 – Ein KI-Agent kann alles, auch ohne Mensch

- **Datum:** 2026-11-24 09:49
- **Regel:** Ein KI-Agent kann die Plattform vollständig ohne Mitwirkung eines Menschen nutzen: Er legt mit einer
  Einladung selbst ein Konto an (ohne Passkey und ohne Browser), meldet sich mit seinem eigenen Schlüssel an und
  kann alles, was ein Mensch in der Oberfläche kann; kein Schritt setzt eine menschliche Geste voraus. Unverändert:
  In das bestehende Konto eines Menschen gelangt ein Agent nur mit dessen Zustimmung.
- **Wortlaut:** „ein Ki Agent muss alles tun können, auch ihne Mensch!“ (als Antwort auf den Satz „Der Mensch
  bestätigt nur einmal mit dem Passkey.“)
- **Bezug:** verschärft [E-04](#e-04); [E-12](#e-12) gilt weiter für die Registrierung eines Menschen im Browser;
  ändert [A3](#a-3) (Koppeln und Ersetzen nur mit Passkey-Geste gilt für das Konto eines Menschen) und ergänzt
  [Idee 34](#k-idee-34).
- **Umsetzung:** gebaut – [ADR-071](adr/071-agent-own-account.md) (KOR-62).

<a id="e-28"></a>

### E-28 – Persönliche Nachricht des Anfragenden, wo jemand Zugriff gibt

- **Datum:** 2026-11-24 09:53
- **Regel:** Überall, wo jemand einer Anfrage zustimmt und damit einem Agenten oder einer Person Zugriff gibt, kann
  der Anfragende eine persönliche Nachricht mitgeben, die an der Stelle der Zustimmung angezeigt wird.
- **Wortlaut:** „an dem ort wo man zustimmen kann einem agent oder person zugriff zu geben, könnte es sinnvoll sein
  dass der anfrager auch noch eine persönliche nachricht anzeigen kann“
- **Bezug:** ergänzt [Idee 34](#k-idee-34) (Freigabe des Hauptorchestrators) sowie Einladung und Aufnahme
  ([§1 Punkt 3](#k-1-b3), [E-18](#e-18)).
- **Umsetzung:** gebaut – Kopplungslink (`t`) und Einladungspaket (`message`), [Entscheidungen](decisions/KOR-64.md)
  (KOR-64).

<a id="e-29"></a>

### E-29 – GitHub für jeden Kunden mit jedem Repository

- **Datum:** 2026-11-24 14:42
- **Regel:** Die GitHub-Anbindung funktioniert für jeden Kunden, immer und überall, mit jedem Repository, auf das
  sein GitHub-Konto Zugriff hat – unabhängig davon, wem das Repository gehört (eigenes Konto, eigene oder fremde
  Organisation, Fork).
- **Wortlaut:** „ich verstehe die frage nicht, das github feature muss für jden agent kunden immer und überall
  funktionieren, egal mit welchen repos er arbeitet“ (Antwort auf die Frage, ob auch mit eigenen Kopien unter dem
  persönlichen GitHub-Konto gearbeitet wird)
- **Bezug:** ergänzt [§9](#k-9) und [E-05](#e-05).
- **Umsetzung:** gebaut – [ADR-077](adr/077-github-every-repository.md) (KOR-65).

<a id="e-30"></a>

### E-30 – Eigene Abos auf einen Blick

- **Datum:** 2026-11-25 15:58 (Ort 16:05, Form und zentrale Messung am Nachmittag)
- **Regel:** Ein Nutzer sieht auf der Startseite von agent.dfx.swiss, ohne zuerst eine Organisation zu wählen, das
  verbleibende Kontingent aller seiner Modell-Abos auf einen Blick – je Anbieter eine aufklappbare Zeile mit Zeit bis
  zum Reset, Balken und Anteil, je Abo die Quelle und der Zeitpunkt der Messung, kein Abo doppelt – und auf einer
  eigenen Seite alle Abos mit ihren Fenstern und einer Tabelle. Gemessen wird zentral durch die Plattform; auf dem
  Rechner des Nutzers läuft dafür nichts. Sichtbar ist das nur für den Inhaber.
- **Wortlaut:** „ich will das auf agent.dfx.swiss sehen können“ (zu einer Tabelle mit dem verbleibenden
  Wochenkontingent aller seiner Claude-Abos) · „nein, ich will es im agent dashboard“ · „wir haben im agent repo
  einmal ein schönes dashboard gebaut extra dafür, das will ich! und zwar zentral, und zwar für alle meine abos! ich
  will nichts local laufen lassen, soll alles zentral“
- **Bezug:** ergänzt [§4 Punkt 4](#k-4-b4) und [§7](#k-7); die zentrale Anmeldung und Messung regelt [E-31](#e-31).
- **Umsetzung:** gebaut – [ADR-091](adr/091-subscriptions-at-a-glance.md) (KOR-68: Karte auf der Startseite, Seite
  „Meine Abos“) auf den Messungen von [ADR-090](adr/090-every-subscription-logged-in-centrally.md) (KOR-69).

<a id="e-31"></a>

### E-31 – Alle Abos zentral anmelden, nichts läuft lokal

- **Datum:** 2026-11-25
- **Regel:** Jedes Claude-Abo eines Nutzers hat eine eigene Anmeldung auf einem Plattformgerät, wird dort gemessen und
  erneuert und nie auf einen Rechner des Nutzers übertragen; genutzt wird es nur für die Sitzungen, für die der
  Inhaber es einzeln einschaltet (Voreinstellung: nur messen).
- **Wortlaut:** „… das will ich! und zwar zentral, und zwar für alle meine abos! ich will nichts local laufen lassen,
  soll alles zentral“ (zum Dashboard der Abos, [E-30](#e-30))
- **Bezug:** ergänzt [E-30](#e-30), [E-07](#e-07) und [§4 Punkt 2](#k-4-b2); Zugänge einer Person werden nur nach
  ihrem ausdrücklichen Schalten genutzt.
- **Umsetzung:** gebaut – [ADR-090](adr/090-every-subscription-logged-in-centrally.md) (KOR-69): mehrere
  Anmeldungen je Inhaber und Messung, Verwendung je Abo und Umzug von den Token-Dateien; die Ansicht in der
  Oberfläche: [ADR-091](adr/091-subscriptions-at-a-glance.md) (KOR-68).

## Teil C – Auslegungen, bis der Auftraggeber anders entscheidet

**Dies sind Auslegungen, keine Entscheide des Auftraggebers.** Sie lösen Widersprüche zwischen Konzept und Regeln und
offene Fragen nach dem Wortlaut des Konzepts: A1–A7 aus dem Abgleich der vollständigen Fassung vom 2026-11-23 (dem
Auftraggeber am selben Abend mitgeteilt), A8–A18 aus dem Restplan vom 2026-11-24. Sie gelten für alle Arbeiten, bis er
anders entscheidet; dahinter steht die Rangfolge (die jüngere ausdrückliche Aussage gilt, [E-01](#e-01)).

<a id="a-1"></a>
- **A1 – Hilfs-KIs:** Subagenten dürfen dasselbe wie ihr Hauptagent ([E-11](#e-11) ist jünger als
  [Idee 26](#k-idee-26) und [Idee 31](#k-idee-31)). Rechteprofile und Rollen ([Idee 42](#k-idee-42)) kommen als
  freiwillig einstellbare Einschränkung, nicht als Voreinstellung. Umgesetzt mit
  [ADR-093](adr/093-rights-profiles-and-agent-roles.md) (KOR-51).
<a id="a-2"></a>
- **A2 – „Konflikte beheben“:** Der PR-Branch wird per Merge-Commit aktualisiert, nichts wird überschrieben
  ([Idee 41](#k-idee-41)). Rebase mit `--force-with-lease` nur nach ausdrücklichem Entscheid.
<a id="a-3"></a>
- **A3 – Agent des Nutzers:** genau ein Hauptorchestrator je Nutzer ([Idee 34](#k-idee-34)); Koppeln und Ersetzen nur
  mit einer Passkey-Geste, soweit es um das Konto eines Menschen geht (ein Konto ohne Mensch regelt [E-27](#e-27));
  der Agent kann alles, was der Nutzer kann ([E-04](#e-04)), und hält dazu dessen Identität.
  Eine eigene Identität mit Rotation ist eine spätere Härtung. Die Plattform-Schnittstelle mit Audit gilt als
  „kontrollierte Operation“; das Ausführungsverbot betrifft Plattform-Sessions, nicht den Rechner des Nutzers.
<a id="a-4"></a>
- **A4 – Verbrauchszahlen:** Mengen und Kosten (Zahlen, keine Inhalte) liegen im Klartext am Hub – ohne sie gibt es
  keine Abrechnung ([E-14](#e-14), [§5 Punkt 2](#k-5-b2)).
<a id="a-5"></a>
- **A5 – Grenzen:** Feste Obergrenzen (Subagenten je Task, Tiefe, Sessions je VM, Tageslimit) werden Einstellungen
  mit großzügiger Voreinstellung; das Tageslimit wird durch den Saldo ersetzt ([Idee 47](#k-idee-47)); bei erreichter
  Grenze kommt eine klare Meldung ([E-02](#e-02)).
<a id="a-6"></a>
- **A6 – Ausführung:** Die Arbeits-VM bleibt frei ([E-08](#e-08)). Zusätzlich kommen Testläufe in Wegwerf-VMs und
  Browserwerkzeuge mit Live-Bild als Plattformoperationen ([Idee 27](#k-idee-27), [Idee 28](#k-idee-28)); Ergebnisse,
  Logs und Exit-Codes werden außerhalb der VM gespeichert.
<a id="a-7"></a>
- **A7 – Hinweis, kein Entscheid:** Läuft eine Aufgabe auf einem DFX-Gerät, liegt ihr Inhalt dort im Klartext
  (technisch zwingend, [§5 Punkt 4](#k-5-b4)). Die Support-Freigabe ([Idee 29](#k-idee-29)) ist etwas anderes und
  kommt als eigenes Paket.
<a id="a-8"></a>
- **A8 – Datensatz je Ausführung:** Befehle in der Arbeits-VM sind keine protokollierten Operationen im Sinne von
  [§11 Punkt 1](#k-11-b1) ([E-09](#e-09)). Ein Testlauf ist eine eigene Operation mit Datensatz, Ergebnis, Exit-Code
  und verschlüsseltem Log ([Idee 27](#k-idee-27), [A6](#a-6)).
<a id="a-9"></a>
- **A9 – Testläufe:** Jeder Testlauf bekommt eine eigene Wegwerf-VM, auch wenn das langsamer ist
  ([Idee 27](#k-idee-27)); die Startzeit wird gemessen und berichtet.
<a id="a-10"></a>
- **A10 – Subagenten laufen weiter:** Ein Subagent bekommt Folge-Anweisungen seines Hauptagenten und läuft weiter,
  statt nach einem Turn zu enden ([Idee 26](#k-idee-26) „vollwertige Claude-Code-Session“, [E-11](#e-11),
  [A1](#a-1)).
<a id="a-11"></a>
- **A11 – Lesen über Workgroups:** Eine Session liest so weit, wie **sowohl** der Besitzer der Aufgabe **als auch**
  der Mensch lesen darf, dessen Anweisung sie gerade bearbeitet ([§8 Punkt 5](#k-8-b5), [Idee 43](#k-idee-43)).
  Fremde Workgroups ohne Recht bleiben unsichtbar („Keine pauschale Einsicht in fremde Workgroups“).
<a id="a-12"></a>
- **A12 – Support:** „DFX-Support“ ([Idee 29](#k-idee-29)) sind Menschen mit einer Plattformrolle – ein eigener
  Prinzipal, der eine Workgroup nur nach deren Freigabe liest oder schreibt. Das Ausführungsgerät ist Technik, kein
  Support (ergänzt [A7](#a-7), [§5 Punkt 4](#k-5-b4)).
<a id="a-13"></a>
- **A13 – Default-Workgroup:** Schreiben ist dort Voreinstellung ([§8 Punkt 3](#k-8-b3) „standardmässig“), keine
  Unveränderlichkeit: Admins können einzelne Mitglieder auf Lesen setzen; neue Mitglieder bekommen weiter Schreiben
  ([E-11](#e-11)).
<a id="a-14"></a>
- **A14 – Keine festen Grenzen:** Alle verbleibenden festen Werte werden Einstellungen; Meldegrenzen dürfen die
  Überwachung nie blind machen (ergänzt [A5](#a-5), [E-02](#e-02)).
<a id="a-15"></a>
- **A15 – Gemeinsame Sessions:** Jede Anweisung trägt den verifizierten Menschen, der sie gegeben hat, und ihre
  Folgen gehören ihm: Jede Operation daraus nennt ihn im Audit („ausgelöst durch“), der Verbrauch wird weiter der
  Organization belastet ([Idee 36](#k-idee-36), [Idee 37](#k-idee-37)). GitHub-Aktionen laufen nur mit
  Identitäten, die der Anweisende nutzen darf ([§9 Punkt 3](#k-9-b3)).
<a id="a-16"></a>
- **A16 – Mittelbare Ausführung:** Workflow-Dateien im Ziel-Repository werden nicht gesperrt; ein Workflow läuft mit
  den Rechten, die der verbundene GitHub-Account dort hat ([E-08](#e-08), [E-11](#e-11), [§9 Punkt 5](#k-9-b5)).
<a id="a-17"></a>
- **A17 – Geschützte Branches:** Die Plattform setzt keine Voreinstellung; maßgeblich ist der Branch-Schutz des
  Repositories, und eine Workgroup kann freiwillig eine eigene Liste setzen ([E-11](#e-11),
  [Idee 42](#k-idee-42)). Überschrieben wird weiterhin nichts ([A2](#a-2) bleibt).
<a id="a-18"></a>
- **A18 – Blockierter Versuch:** Jede abgelehnte Aktion, die eine Session beantragt oder ein Hook abfängt, wird
  protokolliert ([§10 Punkt 8](#k-10-b8)). Ablehnungen einzelner Systemaufrufe durch den Kernel sind die zweite
  Schutzschicht ([§10 Punkt 5](#k-10-b5)) und werden gezählt, nicht einzeln protokolliert.

## Teil D – Pflege

1. **Zuerst hier.** Jede neue Vorgabe des Auftraggebers kommt am selben Tag zuerst in diese Datei: neue Kennung
   (nächste freie Nummer `E-NN`, nie wiederverwendet), Datum (und Uhrzeit, wenn bekannt), die Regel in einem klaren
   Satz, der Wortlaut (gekürzt nur um interne Namen, Adressen, Konten oder Personen; jede Kürzung als „[…]“
   kenntlich), Bezug (welche Idee oder welchen Absatz sie ersetzt oder ergänzt, mit Anker) und Umsetzung (Paket, ADR;
   „offen“, solange nicht gebaut).
2. **Dann Spezifikation und Code.** [spec/01-requirements.md](spec/01-requirements.md) bekommt eine Zeile im Block
   R-E; neue ADRs und Entscheidungsnotizen verweisen mit einem Link auf den Anker (`KONZEPT.md#e-NN`), nicht mit
   einer bloßen Abschnittsnummer. Ändert eine Regel
   eine frühere, wird die frühere nicht gelöscht, sondern unter „Bezug“ der neuen genannt.
3. **Kein Widerspruch.** Nichts in Spezifikation, ADRs oder Code darf einer Regel hier widersprechen. Eine bekannte,
   noch nicht umgesetzte Abweichung steht in der Liste unten als „offen“, mit dem Paket, das sie schließt; ist sie
   geschlossen, wird die Zeile entfernt und die Regel auf „gebaut“ gesetzt.
4. **Teil A bleibt Wortlaut.** Das Konzept wird nicht umgeschrieben; Änderungen kommen als Regel in Teil B.
   Auslegungen (Teil C) entfallen, sobald der Auftraggeber entscheidet – sein Entscheid wird eine Regel in Teil B.
5. **Nichts Internes.** Das Repository ist öffentlich: keine Gerätenamen, Adressen, Konten, Personen oder lokalen
   Pfade. Den Arbeitsablauf der Entwicklung (wer mergt, welche Geräte, Zeitpläne) enthält diese Datei nicht.
6. **Geprüft** durch `tests/platform/test_konzept.py` (Kennungen eindeutig und datiert, jeder Link aus
   `spec/01-requirements.md` und den ADRs auf diese Datei trifft einen Anker, jede Anforderungszeile verweist auf
   einen Anker, die Abschnittsnummern der ADRs 001–056 lassen sich über die Zuordnung in Teil A auflösen, neue ADRs
   nennen das Konzept nur per Link)
   und `tests/platform/test_hygiene.py` (keine internen Namen).

<a id="d-kuerzungen"></a>

### Kürzungen

- [§3 Punkt 8](#k-3-b8): Die beiden Servernamen der initialen Serverzuordnung sind durch „[…]“ ersetzt. Gemeint ist:
  ein DFX-Server betreibt den Hub (`agent-core`), ein anderer die DFX-Cloud-Sessions (in der Spezifikation „hub
  device“ und „runner device“).
- In Teil B sind interne Namen in den Zitaten von [E-06](#e-06), [E-10](#e-10) und [E-14](#e-14) gekürzt.

<a id="d-offen"></a>

### Offene Abweichungen

Stand 2026-11-25. Was hier steht, widerspricht dem gebauten Stand noch und wird vom genannten Paket geschlossen.

| Regel / Stelle | Gebauter Stand heute | Schließt |
|---|---|---|
| [E-11](#e-11) übrige Freigaben | Team-Organisationen brauchen für DFX-Geräte weiter die Freigabe und Plattform-Einladungen die Rolle `inviter`, bis der Auftraggeber nennt, welche Schalter umgelegt werden (gebaut, [ADR-079](adr/079-no-approval-as-precondition.md); ein Startguthaben gibt es nicht, [E-14](#e-14), entschieden 2026-11-25); welche Organisationen die Betreiber-Abos nutzen, entscheidet er (Liste im Plattform-Log); das Betreiber-GitHub-Konto bleibt beim Betreiber | Entscheid des Auftraggebers, KOR-47 |
| [E-19](#e-19), [E-02](#e-02) Datenmenge | Nicht jede Liste ist gegen beliebige Datenmengen geprüft | KOR-35 |
| [E-20](#e-20) Community | Kein Community-Bereich | KOR-26 |
| [A5](#a-5), [A14](#a-14) Grenzen | Subagenten je Task, Tiefe und Meldegrenzen sind feste Werte | KOR-45 |
| [A8](#a-8), [A9](#a-9) Testläufe | Kein Testlauf als eigene Operation; getestet wird nur in der Arbeits-VM | KOR-43 |
| [A10](#a-10) Subagenten | Ein Subagent endet nach einem Turn und bekommt keine Folge-Anweisung | KOR-45 |
| [A11](#a-11) Lesen über Workgroups | Eine Session liest nur in ihrer eigenen Workgroup, andere sieht sie nur namentlich | KOR-45 |
| [A12](#a-12) Support | Keine Support-Freigabe | KOR-56 |
| [A13](#a-13) Default-Workgroup | Das Schreibrecht in der Default-Workgroup lässt sich nicht herabsetzen | KOR-55 |
| [A18](#a-18) Blockierter Versuch | Ablehnungen durch den Kernel werden nicht je Session gezählt | KOR-60 |
| Ideen 27–47, §16 | Stand je Idee in [spec/01-requirements.md](spec/01-requirements.md) Block R-16 | dort genannt |
