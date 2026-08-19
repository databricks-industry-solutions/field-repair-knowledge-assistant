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



# TX Falfurrias  – US281 NB -Kistler - Prepass reporting  0 Weight , 0 Classification and 0 Spacing

## Number
R&DTASK0001002
## Parent
SDC0001005
## Assignment group
R&D
## Assigned to
Cedar Mah

## Priority

2 - High
## Status

Closed Incomplete
## Workflow Status

Completed
## Follow up
## Location
TX Falfurrias US-281 NB

## Description
Issue: Prepass reporting  0 Weight , 0 Classification and 0 Spacing


Investigation- 1. Using the suspect vehicles time stamps provided by PrePass,  looked for and compared Kistler data in the corresponding SRIS vehicle events
	       2. Verified Overview image of vehicles in Vehicle Live summary for lane changes, non commercial vehicles, off scale situations
               3. Searched Kistlers historical records for time stamps 4. Checked SmartLoop detections

Findings-  1. Kistler data on Vehicle Live Summary do show 0 GVW, 0 axle and class 15 for the matched vehicle records
           2. Kistler records show - 1 lb for the matched vehicle records 3. SmartLoop shows loop counts are identical  4. SensorEdgeDriving is common 5.  There are some images that show vehicles centered but still get SensorEdgeDriving warnings



SRIS Cabinet-PrePass WIM Interface Install, Site - 


Brian,
Can you have a look at the Falfurrias NB TX WIM.  There appears to be a problem.
vehicle classification is 0, missing vehicle lengths, missing axle spacings.
This is the raw wim data file to PrePass.
[A screenshot of a computer    Description automatically generated]

[cid:image002.png@01DB303E.B59EF240]




[cid:hhRri9Ri0UmWBdeLyiGVQNEW-PP_Vert_Color_RGB_png]    David Covington
Field Service Systems Technician Supervisor
David.Covington@prepass.com
Mobile: 352-467-4340
www.prepass.com<http://www.prepass.com/>

## Notes
Issue: Prepass reporting  0 Weight , 0 Classification and 0 Spacing

2024-12-10 cm
Looked up 3 records using the DOT # and the corresponding records all had weights associated with them, so the wim itself is working.
The only other interface between IRD and us is the SRA.
Also noticed that there were some '0' weights in DW SRIS Live Summary






  



Investigation- 1. Using the suspect vehicles time stamps provided by PrePass,  looked for and compared Kistler data in the corresponding SRIS vehicle events
           2. Verified Overview image of vehicles in Vehicle Live summary for lane changes, non commercial vehicles, off scale situations
               3. Searched Kistlers historical records for time stamps

Findings-  1. Kistler data on Vehicle Live Summary do show 0 GVW, 0 axle and class 15 for the matched vehicle records
           2. Kistler records show - 1 lb for the matched vehicle records  3.  Many messages are SensorEdgeDriving 



## Close Notes
Looked up 3 records using the DOT # and the corresponding records all had weights associated with them, so the wim itself is working.
The only other interface between IRD and us is the SRA.
Also noticed that there were some '0' weights in DW SRIS Live Summary


## Activities
cmah@drivewyze.com
Image uploaded•2024-12-10 11:55:40

cmah@drivewyze.com
Image uploaded•2024-12-10 11:46:00

cmah@drivewyze.com
Image uploaded•2024-12-10 11:45:45

cmah@drivewyze.com
Image uploaded•2024-12-10 11:45:26

cmah@drivewyze.com
Image uploaded•2024-12-10 11:42:58

dskinner@drivewyze.com
Field changes•2024-11-08 14:43:55
Assigned toCedar Mah
Impact3 - Low
Opened byDavid Skinner
Priority2 - High
StatusClosed Incomplete

dskinner@drivewyze.com
Attachment uploaded•2024-11-08 14:43:37
2024-11-07 TX Falfurias PrePass Kistler Data Issue.xlsx1.01 MB

dskinner@drivewyze.com
Attachment uploaded•2024-11-08 14:30:35
RE_ TX Falfurias WIM Investigation - David Skinner - Outlook.pdf1.83 MB



# TN Haywood I-40 EB - The USB Alpr camera is down

## Number
R&DTASK0001029
## Parent
SDC0001813
## Assignment group
R&D
## Assigned to
Cedar Mah

## Priority

3 - Moderate
## Status

Closed Incomplete
## Workflow Status

Assigned
## Follow up
## Location
TN Haywood I-40 EB

## Description
A new USB camera, cable and Hts computer has been pulled, testing is required before shipping them out.

## Notes
2025-11-13 - Vipul - Cedar has tested USB cameras, but they don't seem to work...his recommendation is to replace with IP camera. Task has been assigned to Engineering Services team.

## Close Notes
Cedar has tested USB cameras, but they don't seem to work...his recommendation is to replace with IP camera. Task has been assigned to Engineering Services team.

## Activities
Vipul Chavda
Additional comments•2025-11-13 11:47:46
@Cedar Mah can you please give us an update on this?

Vipul Chavda
Additional comments•2025-10-09 11:35:53
@Cedar Mah we need recommendation on this, please!


Dara Ola
Field changes•2025-08-21 09:52:31
Assigned toCedar Mah
Impact3 - Low
Opened byDara Ola
Priority3 - Moderate
StatusClosed Incomplete





# TX Loving County 302 WB - WIM Weights High

## Number
R&DTASK0001033
## Parent
SDC0002419
## Assignment group
R&D
## Assigned to
Cedar Mah


## Priority

3 - Moderate
## Status

Closed Skipped
## Workflow Status

Assigned
## Follow up
## Location
TX Loving County 302WB

## Description
1.	Detailed description/ Summary of issue
Corporal Arnold Reeves called to let us know the WIM weights are about 6,000 lbs off (high) and he says tandem axles are counting as single axles.

2.	What is the impact to the customer?
More trucks than necessary are being pulled in.

3.	What troubleshooting steps were taken to verify and or resolve issue? 
N/A

4.	Recommendation to resolve issue (parts required etc.)
Investigate this issue. I don't know if this will require a recalibration.

## Notes
2025-10-21 Bowen - Followed up with Cedar

2025-10-09 #AYO# Cedar confirmed He was going to update the kistler firmware so that He can take a closer look at the wim data as well, but got distracted.  He said he will do that today, or tonight when traffic is lighter.

## Close Notes
We are not sure what we did to fix this. But the station confirmed that the data is better than before.

## Activities
Vipul Chavda
Additional comments•2025-11-13 11:53:02
@Cedar Mah we are closing this case. My team expects the assignee to close these tasks but since this was still open, I will close it.


Ayotunde Obawole
Field changes•2025-10-07 15:56:05
Assigned toCedar Mah
Impact3 - Low
Opened byAyotunde Obawole
Priority3 - Moderate
StatusClosed Skipped















# NM Texico US-60/US-70/US-84 WB-HTS Controller Web  Application keeps crashing

## Number
R&DTASK0001017
## Parent
SDC0001253
## Assignment group
R&D
## Assigned to
Cedar Mah

## Priority

2 - High
## Status

Closed Skipped
## Workflow Status

Assigned
## Follow up
## Location
NM Texico US-60/US-70/US-84 WB

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
2025-03-31-Eduardo

Support not required anymore. HTS system is now working fine.

## Activities
Eduardo Cadelina
Field changes•2025-02-21 10:24:59
Assigned toCedar Mah
Impact3 - Low
Opened byEduardo Cadelina
Priority2 - High
StatusClosed Skipped

Eduardo Cadelina
Image uploaded•2025-02-21 10:24:44



# DE 301 - Mainline Right OVC Intermittently Not Connecting

## Number
R&DTASK0001036
## Parent
SDC0002304
## Assignment group
R&D
## Assigned to
Sangwon Lim

## Priority

3 - Moderate
## Status

Closed Incomplete
## Workflow Status

Assigned
## Follow up
## Location
DE Middletown RT-301

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
2025-10-24 Bowen - Sangwon and I talked about creating a keep alive thread, pull a picture every X minutes to keep the camera from idling - message thread in case.

2025-10-23 Bowen - This issue got progressively more frequent. The OVC would go out every few days. Within a couple weeks, it was going out multiple times a day. During this time, Sangwon checked the logs and found nothing meaningful. He changed the settings and updated the firmware. The camera worked for nearly 2 weeks. It has since started acting up. Currently it experiences this error every 1-2 days. Sangwon disabled the motion recognition. If this does not work, he will reset the camera to factory settings and set it up again.

## Close Notes
2025-11-13 sl Replacing the OVC camera as no solution has been found.

## Activities
Bowen Butler
Field changes•2025-10-23 07:01:02
Assigned toSangwon Lim
Impact3 - Low
Opened byBowen Butler
Priority3 - Moderate
StatusClosed Incomplete