# Title

## Number
## Parent
## Assignment group
## Assigned to

## Priority

## Status
## Workflow Status
## Follow up
## Location

## Description


## Notes


## Close Notes

## Activities



# TX Belmont  – US281 NB - Veridyne - BypassPro reporting  0 Weight , 0 Classification and 0 Spacing

## Number
R&DTASK0001002
## Parent
SDC0001005
## Assignment group
R&D
## Assigned to
Priya Raman

## Priority

2 - High
## Status

Closed Incomplete
## Workflow Status

Completed
## Follow up
## Location
TX Belmont US-281 NB

## Description
Issue: BypassPro reporting  0 Weight , 0 Classification and 0 Spacing


Investigation- 1. Using the suspect vehicles time stamps provided by BypassPro,  looked for and compared Veridyne data in the corresponding SRIS vehicle events
	       2. Verified Overview image of vehicles in Vehicle Live summary for lane changes, non commercial vehicles, off scale situations
               3. Searched Veridynes historical records for time stamps 4. Checked LoopSense detections

Findings-  1. Veridyne data on Vehicle Live Summary do show 0 GVW, 0 axle and class 15 for the matched vehicle records
           2. Veridyne records show - 1 lb for the matched vehicle records 3. LoopSense shows loop counts are identical  4. SensorEdgeDriving is common 5.  There are some images that show vehicles centered but still get SensorEdgeDriving warnings



SRIS Cabinet-BypassPro WIM Interface Install, Site - 


Sam,
Can you have a look at the Belmont NB TX WIM.  There appears to be a problem.
vehicle classification is 0, missing vehicle lengths, missing axle spacings.
This is the raw wim data file to BypassPro.
[image: raw WIM data screenshot]




[image: BypassPro contact card]    Jordan Blake
Field Service Systems Technician Supervisor
jblake@example.com
Mobile: 555-0142

## Notes
Issue: BypassPro reporting  0 Weight , 0 Classification and 0 Spacing

2024-12-10 pr
Looked up 3 records using the DOT # and the corresponding records all had weights associated with them, so the wim itself is working.
The only other interface between the WIM vendor and us is the SRA.
Also noticed that there were some '0' weights in the SRIS Live Summary






  



Investigation- 1. Using the suspect vehicles time stamps provided by BypassPro,  looked for and compared Veridyne data in the corresponding SRIS vehicle events
           2. Verified Overview image of vehicles in Vehicle Live summary for lane changes, non commercial vehicles, off scale situations
               3. Searched Veridynes historical records for time stamps

Findings-  1. Veridyne data on Vehicle Live Summary do show 0 GVW, 0 axle and class 15 for the matched vehicle records
           2. Veridyne records show - 1 lb for the matched vehicle records  3.  Many messages are SensorEdgeDriving 



## Close Notes
Looked up 3 records using the DOT # and the corresponding records all had weights associated with them, so the wim itself is working.
The only other interface between the WIM vendor and us is the SRA.
Also noticed that there were some '0' weights in the SRIS Live Summary


## Activities
praman@example.com
Image uploaded•2024-12-10 11:55:40

praman@example.com
Image uploaded•2024-12-10 11:46:00

praman@example.com
Image uploaded•2024-12-10 11:45:45

praman@example.com
Image uploaded•2024-12-10 11:45:26

praman@example.com
Image uploaded•2024-12-10 11:42:58

dsherman@example.com
Field changes•2024-11-08 14:43:55
Assigned toPriya Raman
Impact3 - Low
Opened byDan Sherman
Priority2 - High
StatusClosed Incomplete

dsherman@example.com
Attachment uploaded•2024-11-08 14:43:37
2024-11-07 TX Belmont BypassPro Veridyne Data Issue.xlsx1.01 MB

dsherman@example.com
Attachment uploaded•2024-11-08 14:30:35
RE_ TX Belmont WIM Investigation - Dan Sherman - Outlook.pdf1.83 MB



# TN Bedford I-40 EB - The USB Alpr camera is down

## Number
R&DTASK0001029
## Parent
SDC0001813
## Assignment group
R&D
## Assigned to
Priya Raman

## Priority

3 - Moderate
## Status

Closed Incomplete
## Workflow Status

Assigned
## Follow up
## Location
TN Bedford I-40 EB

## Description
A new USB camera, cable and Hts computer has been pulled, testing is required before shipping them out.

## Notes
2025-11-13 - Anil - Priya has tested USB cameras, but they don't seem to work...her recommendation is to replace with IP camera. Task has been assigned to Engineering Services team.

## Close Notes
Priya has tested USB cameras, but they don't seem to work...her recommendation is to replace with IP camera. Task has been assigned to Engineering Services team.

## Activities
Anil Kapoor
Additional comments•2025-11-13 11:47:46
@Priya Raman can you please give us an update on this?

Anil Kapoor
Additional comments•2025-10-09 11:35:53
@Priya Raman we need recommendation on this, please!


Nadia Farah
Field changes•2025-08-21 09:52:31
Assigned toPriya Raman
Impact3 - Low
Opened byNadia Farah
Priority3 - Moderate
StatusClosed Incomplete





# TX Delmar County 302 WB - WIM Weights High

## Number
R&DTASK0001033
## Parent
SDC0002419
## Assignment group
R&D
## Assigned to
Priya Raman


## Priority

3 - Moderate
## Status

Closed Skipped
## Workflow Status

Assigned
## Follow up
## Location
TX Delmar County 302WB

## Description
1.	Detailed description/ Summary of issue
Sergeant Alan Pierce called to let us know the WIM weights are about 6,000 lbs off (high) and he says tandem axles are counting as single axles.

2.	What is the impact to the customer?
More trucks than necessary are being pulled in.

3.	What troubleshooting steps were taken to verify and or resolve issue? 
N/A

4.	Recommendation to resolve issue (parts required etc.)
Investigate this issue. I don't know if this will require a recalibration.

## Notes
2025-10-21 Owen - Followed up with Priya

2025-10-09 #KOFI# Priya confirmed She was going to update the Veridyne firmware so that She can take a closer look at the wim data as well, but got distracted.  She said she will do that today, or tonight when traffic is lighter.

## Close Notes
We are not sure what we did to fix this. But the station confirmed that the data is better than before.

## Activities
Anil Kapoor
Additional comments•2025-11-13 11:53:02
@Priya Raman we are closing this case. My team expects the assignee to close these tasks but since this was still open, I will close it.


Kofi Mensah
Field changes•2025-10-07 15:56:05
Assigned toPriya Raman
Impact3 - Low
Opened byKofi Mensah
Priority3 - Moderate
StatusClosed Skipped









# NM Clearwater US-60/US-70/US-84 WB-HTS Controller Web  Application keeps crashing

## Number
R&DTASK0001017
## Parent
SDC0001253
## Assignment group
R&D
## Assigned to
Priya Raman

## Priority

2 - High
## Status

Closed Skipped
## Workflow Status

Assigned
## Follow up
## Location
NM Clearwater US-60/US-70/US-84 WB

## Description
1.	Detailed description/ Summary of issue
HTS Controller Web  Application keeps crashing
2.	What is the impact to the customer
No ALPR images and truck decodes being displayed in the live summary
3.	What troubleshooting steps were taken to verify and or resolve issue 
Kill HTS Controller Web App. Power down the HTS computer.
This is the same issue occurred Last year SDC0001019
4.	Recommendation to resolve issue (parts required etc.) 
Not sure yet
5.	Was issue resolved
No

## Notes


## Close Notes
2025-03-31-Diego

Support not required anymore. HTS system is now working fine.

## Activities
Diego Herrera
Field changes•2025-02-21 10:24:59
Assigned toPriya Raman
Impact3 - Low
Opened byDiego Herrera
Priority2 - High
StatusClosed Skipped

Diego Herrera
Image uploaded•2025-02-21 10:24:44



# DE 301 - Mainline Right OVC Intermittently Not Connecting

## Number
R&DTASK0001036
## Parent
SDC0002304
## Assignment group
R&D
## Assigned to
Marcus Webb

## Priority

3 - Moderate
## Status

Closed Incomplete
## Workflow Status

Assigned
## Follow up
## Location
DE Fairport RT-301

## Description
1.	Detailed description/ Summary of issue
The OVC camera link status is changing to No Link. This is an infrequent occurrence but seems to require a reboot for >10 minutes to recover.

2.	What is the impact to the customer?
Occassional no OVC images

3.	What troubleshooting steps were taken to verify and or resolve issue? 
Checked the logs. This seemed to occur at the same time as a VWI issue which was resolved by restarting the SRA's which in turn likely restarted the VWI bars. This is being monitored.

4.	Recommendation to resolve issue (parts required etc.)
See if this happens again. If it does, see if it is related to the prior issue or if it is an isolated issue. If it happens again, I may see if R&D recommend checking for firmware updates.

## Notes
2025-10-24 Owen - Marcus and I talked about creating a keep alive thread, pull a picture every X minutes to keep the camera from idling - message thread in case.

2025-10-23 Owen - This issue got progressively more frequent. The OVC would go out every few days. Within a couple weeks, it was going out multiple times a day. During this time, Marcus checked the logs and found nothing meaningful. He changed the settings and updated the firmware. The camera worked for nearly 2 weeks. It has since started acting up. Currently it experiences this error every 1-2 days. Marcus disabled the motion recognition. If this does not work, he will reset the camera to factory settings and set it up again.

## Close Notes
2025-11-13 mw Replacing the OVC camera as no solution has been found.

## Activities
Owen Brooks
Field changes•2025-10-23 07:01:02
Assigned toMarcus Webb
Impact3 - Low
Opened byOwen Brooks
Priority3 - Moderate
StatusClosed Incomplete
