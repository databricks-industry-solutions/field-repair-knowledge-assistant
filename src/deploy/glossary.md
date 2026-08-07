# FIS R&D Acronym & Term Glossary

Neutral reference definitions for acronyms and product/component names that recur
across the FIS (Fleetworthy Infrastructure Solutions) R&D ticket corpus. Entries
are short, factual, and vendor/component oriented. They are **not** phrased against
any specific ticket or expected answer — a Knowledge Assistant should treat these
as one grounding source among many (corpus co-occurrence is the other), hedge when
a term is ambiguous in context, and always cite.

## Controllers & Applications

- **CA — Controller Application.** The control/operator software layer for a
  roadside HTS installation; a component of the HTS controller stack. Sometimes
  referenced simply as "the CA" when engineers mean the controller app rather than
  the physical controller.
- **HTS.** Highway/roadside controller platform that hosts the Controller
  Application (CA) and coordinates attached sensors, cameras, and the web app.
- **SmartLoop.** Inductive-loop vehicle-detection subsystem used for presence and
  counting at a lane/site.
- **SRA.** Site/roadside assembly reference used in installation and service
  records.

## Weigh-in-Motion

- **WIM — Weigh-In-Motion.** System that measures axle/vehicle weights while a
  vehicle is moving over in-road sensors. Failures commonly present as zero or
  implausible weight readings.
- **Kistler.** Sensor vendor whose quartz/piezo strip sensors are used in WIM
  installations. "Kistler sensor" typically refers to a WIM load sensor.
- **axle sensor.** In-road sensor that detects/weighs individual axles; an input to
  the WIM computation.

## Cameras & Imaging

- **AUR.** Camera unit referenced in imaging/ALPR contexts.
- **OVC — Bosch camera.** Camera unit supplied by Bosch (OVC designation) used in
  roadside imaging.
- **ALPR — Automatic License Plate Recognition.** Capability (and the cameras
  serving it) that reads plate characters from vehicle images.
- **PIPS / Neology.** Vendor(s) associated with ALPR cameras and readers
  (PIPS Technology, part of Neology).
- **Vimba.** Camera viewer / SDK tooling (Allied Vision) used to view or configure
  machine-vision camera streams.
- **illuminator.** IR/visible lighting unit paired with a camera to enable capture
  in low light.

## Power & Networking

- **WPS = NetBooter.** Networked power controller (a "NetBooter" unit) used to
  remotely power-cycle roadside equipment. "WPS" and "NetBooter" refer to the same
  power-controller role in these tickets.

## Roadside Data / Traveler Info

- **ATIS — Advanced Traveler Information System.** System that disseminates traffic
  / traveler information; referenced where roadside data feeds are involved.

## Notes on usage

- These definitions describe **roles and vendors**, not fixed answers. An acronym
  may be used loosely in a ticket; resolve meaning from surrounding context and
  co-occurring tickets, then cite both this glossary and the corpus evidence.
- No definition here is authoritative over what a specific ticket actually says.
