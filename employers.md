# Jalisco & Remote-Mexico Employer Radar (`companies.json`)

Updated November 2026. `companies.json` now tracks **106 verified employer boards** across **7 active public JSON ATS engines** (8 supported in `fetch_jobs.py`: **Workday**, **Greenhouse**, **SmartRecruiters**, **Lever**, **Ashby**, **Oracle Recruiting Cloud (`oracle_hcm`)**, **Eightfold AI (`eightfold`)**, and **Workable**).

## 1. Geographic Coverage (`jalisco_locations` + Remote Mexico)

Every daily run scans across:
- **Zona Metropolitana de Guadalajara (ZMG)**: Guadalajara, Zapopan, San Pedro Tlaquepaque, Tonalá, Tlajomulco de Zúñiga, El Salto, Ixtlahuacán de los Membrillos, Juanacatlán, Acatlán de Juárez, Zapotlanejo.
- **Jalisco Regional Hubs**: Amatitán & Tequila (spirits/CPG supply chain), Atotonilco el Alto, Tepatitlán, Lagos de Moreno (automotive corridor), Ciudad Guzmán / Zapotlán, Puerto Vallarta.
- **Remote-Mexico & Country-Wide Listings**: Roles tagged `Remote - Mexico` or `Mexico` from tech, fintech, consulting, and enterprise cloud hubs that hire remotely from Jalisco.

---

## 2. Tracked Employers by Industry Cluster (106 Active Boards)

| Cluster | Count | Tracked Employers (`companies.json`) |
|---|---:|---|
| **EMS, Hardware & Smart Manufacturing** | 7 | Flex, Jabil, Plexus, Molex, Avnet, Bright Machines, Mill |
| **Semiconductors & Silicon Design** | 11 | Intel, NXP Semiconductors, Micron, Solidigm, Western Digital, Qualcomm, Nvidia, Broadcom, Marvell, Analog Devices, Microchip Technology |
| **Enterprise Tech, Cloud & Telecom Hubs** | 10 | Oracle (MDC Zapopan), HP Inc., Hewlett Packard Enterprise (HPE), Dell Technologies, Toshiba Global Commerce Solutions, Kyndryl, DXC Technology, F5, Ciena, Zebra Technologies |
| **Digital Engineering, AI & Nearshore Consulting** | 11 | Wizeline, Globant, Encora, Improving, Endava, Nagarro, Accenture, Bluelight Consulting, Avertium, Publicis Groupe, Teleperformance |
| **Automotive, Mobility & Industrial Automation** | 10 | Bosch (GDL R&D), BorgWarner (El Salto), Aptiv, Ford Motor Company, Honeywell, Johnson Controls, GE Vernova, Eaton, 3M, O-I (Owens-Illinois) |
| **Pharma, MedTech, Life Sciences & AgriTech** | 13 | AstraZeneca (GITC GDL), Baxter (GDL IT/GBS), Insulet (GDL GCC), Medtronic, Abbott, Roche, Pfizer, Eli Lilly, Sanofi, BD (Becton Dickinson), Thermo Fisher Scientific, Bayer, Corteva Agriscience (Tlajomulco) |
| **CPG, Spirits & Global Business Services (GBS)** | 5 | Brown-Forman (Casa Herradura), Diageo (Zapopan / Atotonilco), Mondelēz International, Kimberly-Clark, Procter & Gamble |
| **Fintech, Banking & Payments** | 15 | Kueski (GDL HQ), Clara, Paystand (GDL), Backbase (GDL), Clip (GDL), Bitso, Nubank, Stripe, Citi (GDL), JPMorgan Chase, American Express, Visa, Mastercard, PayPal, Coinbase |
| **3PL, FreightTech & Supply Chain** | 3 | Rapido Solutions Group (Zapopan), Arrive Logistics (GDL), Flexport (GDL) |
| **Global SaaS & Remote-Mexico Tech** | 21 | Diligent (GDL), ServiceNow, Salesforce, Workday, Adobe, Autodesk, Zendesk, CrowdStrike, Datadog, Okta, Zscaler, Cloudflare, Twilio, GitLab, Canonical, Pinterest, Lyft, HubSpot, Deel, Zapier, Notion |

---

## 3. Custom / Proprietary Portals (Manual Spot-Check Only)

A small subset of Jalisco employers run custom frontends, SAP SuccessFactors HTML portals, Avature, or authenticated APIs without an open unauthenticated JSON feed:
- **Sanmina, Celestica, Foxconn, USI (Universal Scientific Industrial), Benchmark Electronics** (EMS plants in Guadalajara / El Salto / Tonalá)
- **IBM, Cisco, Continental / Aumovio, Visteon, Harman, TE Connectivity, Tata Consultancy Services (TCS), Capgemini, Softtek, EPAM**
- **PepsiCo (`pepsicojobs.com`), José Cuervo / Becle, Grupo Pisa, Grupo Omnilife / Chivas, Megacable, Farmacias Guadalajara**

## 4. Health-Checking Boards

Run `python3 fetch_jobs.py --check` locally or trigger **Refresh job board & send daily email** in GitHub Actions with `check_only = true` to verify response status and Jalisco job counts across all 106 boards without sending emails. Any individual board that returns an error during a normal daily run is logged as a warning and skipped gracefully while preserving previously discovered roles.
