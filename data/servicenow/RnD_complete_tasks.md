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




# MS Cypress - Replace ATIS Enclosures

## Number
R&DTASK0001066
## Parent
SDC0003530
## Assignment group
R&D
## Assigned to
Priya Raman

## Priority

3 - Moderate
## Status

Closed Complete
## Workflow Status

Draft
## Follow up
## Location
MS Cypress I-10

## Description
Please help aim both cameras after both enclosures are replaced.
1.	Detailed description/ Summary of issue
The passenger ATIS camera occasionally gets too hot and shuts down due to high temperature.

2.	What is the impact to the customer?
Passenger ATIS camera turns off.

3.	What troubleshooting steps were taken to verify and or resolve issue? 
Checked WPS logs, the relay turned off. Enabled thermal monitoring to log the temperatures instead of just turning it off and not logging.

4.	Recommendation to resolve issue (parts required etc.)
Inspect the enclosure and the temp probes for accuracy and cooling function.
## Notes

## Close Notes
2026-07-09 ATIS enclosures on both driver and passenger sides are replace and aligned.

## Activities
Owen Brooks
Field changes•2026-06-17 11:53:57
Assigned toPriya Raman
Impact3 - Low
Opened byOwen Brooks
Priority3 - Moderate
StatusClosed Complete







# MS Cypress I-10 - ATIS Passenger Camera Losing IP

## Number
R&DTASK0001049
## Parent
SDC0003066
## Assignment group
R&D
## Assigned to
Marcus Webb

## Priority

3 - Moderate
## Status

Closed Complete
## Workflow Status

Assigned
## Follow up
## Location
MS Cypress I-10

## Description
Please investigate the ATIS passenger camera IP reverting.

1.	Detailed description/ Summary of issue
The Falcon camera in the ATIS JBOX for Passenger side at Cypress is losing IP again.

2.	What is the impact to the customer?
Camera loses connection to the computer.

3.	What troubleshooting steps were taken to verify and or resolve issue? 
We have reset the IP multiple times. The IP Address switches from persistent to DHCP and assigns a random IP Address. We have already switched to a USB Ethernet adapter in case the NIC was bad on the computer.

4.	Recommendation to resolve issue (parts required etc.)
Work with R&D to check the camera and network configuration.

## Notes
2026-04-09 - Anil - Priya to take a look at this issue. It has been working for over a month now. It doesn't work apparently after power issues. Kane has had a look but it will be a good idea for Priya to check it out as well.
When it fails, GigE or CamView says "failsafe" in the name of the camera. Check for possible IP conflict on bootup.

2026-02-17 MW
Plan for next visit to Cypress MS:
Remove the generic USB adapter connection. The adapter only allows Jumbo Packet to 4kb.
Invert the passenger (P4) and driver (P1) side connections
If the issue comes back after inverting the connections
If the issue is on the driver side (Port Issue)
Enable Jumbo Packet on P4. It is currently left disabled for troubleshooting purpose.
If enabling Jumbo Packet does not solve the issue, try setting the connection to another port.
If the issue remains on the passenger side (Camera issue)
The camera needs to be sent back and investigated.
Potentially requires a firmware update.

## Close Notes
2026-06-12 The core of the issue is related to AC failure/mis-wiring.

## Activities
Owen Brooks
Field changes•2026-02-17 08:48:00
Assigned toMarcus Webb
Impact3 - Low
Opened byOwen Brooks
Priority3 - Moderate
StatusClosed Complete




# VA Brookfield NB - Lumex ALPR Installation and Alignment - AWAITING STATE TO FIX NETWORK

## Number
R&DTASK0001048
## Parent
SDC0002324
## Assignment group
R&D
## Assigned to
Priya Raman

## Priority

3 - Moderate
## Status

Closed Complete
## Workflow Status

Draft
## Follow up
## Location
VA Brookfield I-81 NB

## Description
Aim the Lumex ALPR  once installed.


1.	Detailed description/ Summary of issue
The ALPR is offline, no images are being gathered.

2.	What is the impact to the customer?
The customer is unable to get matches against their hotlist and no ALPR images.

3.	What troubleshooting steps were taken to verify and or resolve issue? 
I tried restarting the camera from the WPS. It is only possible to restart by restarting the NTSW and the ALPR since they share a powersupply.

4.	Recommendation to resolve issue (parts required etc.)
A site visit is likely required.

## Notes
2026-03-12 Sherman  Installation date tbd- likely in the next 2 weeks.. Field tech to contact R&D

## Close Notes
2026-06-12 marcus Lumex ALPR replaces old aptix camera. No hardware trigger; thus the image trigger is used.

## Activities
Owen Brooks
Field changes•2026-02-04 09:29:58
Assigned toPriya Raman
Impact3 - Low
Opened byOwen Brooks
Priority3 - Moderate
StatusClosed Complete






# NM Clearwater US-60/US-70/US-84 WB- HTS Controller Web Application keeps going offline

## Number
R&DTASK0001006
## Parent
SDC0001019
## Assignment group
R&D
## Assigned to
Priya Raman

## Priority

3 - Moderate
## Status

Closed Complete
## Workflow Status

Assigned
## Follow up
## Location
NM Clearwater US-60/US-70/US-84 WB


## Description
-Issue found out during a daily routine check. 
-Tried restarting the controller web app and hts services. It will work but after few minutes it will go offline by itself.

## Notes


## Close Notes
Hi Priya,

Just want to update you on where Diego and I were at. We got the latest version of CA installed, as well as the latest version of the recognition model and dataset. We watched it stay live & stable for about 20 minutes, and him & I agreed that we'll get the ticket closed this evening, unless anything else occurs in the meantime.

Now, as him & I discussed, and I believe was relayed to you, while we're hoping the latest version of CA resolves our issue, the error did indicate an issue with Windows' NTP and Time services, meaning that if the latest version of the software continues to pose problems, the next step would be re-installing Windows on the machine. Obviously, this is a bridge that'll only need to be crossed if we come to it, but it's important to note nonetheless.

Of course, if there's anything else I can assist with, or questions I can answer, just reach out and I'll be happy to assist.


## Activities
Diego Herrera
Work notes•2024-12-23 11:00:09
@Priya Raman
@Anil Kapoor

We are still having the same issue. Priya could you please take a look on this one as the station already reaching out.

akapoor@example.com
Work notes•2024-12-18 17:00:29
@Priya Raman I reinstalled VC++ redistributable on HTS machine. Seems to be working now. We will keep an eye on this for another day.

Diego Herrera
Work notes•2024-12-11 11:42:53
@Priya Raman
@Anil Kapoor

Hi Priya did you had a chance to take a look the issue?

Diego Herrera
Field changes•2024-11-19 10:46:14
Assigned toPriya Raman
Impact3 - Low
Opened byDiego Herrera
Priority3 - Moderate
StatusClosed Complete

Diego Herrera
Image uploaded•2024-11-19 10:45:40










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






# NM Sandia I-40 EB POE- WIM/ATPS  outputs lots of warnings almost every trucks requires investigation

## Number
R&DTASK0001055
## Parent
SDC0003291
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
NM Sandia I-40 EB

## Description
1.	Detailed description/ Summary of issue
NM Sandia I-40 WB- WIM/ATPS  outputs lots of warnings almost every trucks

2.	What is the impact to the customer?
Officers  got confuse on what  are these warning for.

3.	What troubleshooting steps were taken to verify and or resolve issue? 
DOT patched the deteriorating are of the ramp in front of the SRIS cabinet

4.	Recommendation to resolve issue (parts required etc.)
Check the WIM/ATPS if need to be recalibrate.

## Notes
2026-06-12 pr - Installed new sensors but was never calibrated.  Believe there are pavement issues.  closing task until wim calibration has been completed

2026-04-09 - Diego - Maybe Priya can help validate/recalibrate the ATPS at this site. Robin will be at the site next week. Diego will provide exact date for Robin's Sandia visit.



## Close Notes
closing task until wim calibration has been completed

## Activities
Diego Herrera
Field changes•2026-04-01 11:55:16
Assigned toPriya Raman
Impact3 - Low
Opened byDiego Herrera
Priority3 - Moderate
StatusClosed Incomplete

Diego Herrera
Image uploaded•2026-04-01 11:55:05

Diego Herrera
Image uploaded•2026-04-01 11:54:34









# NM Rio Seco I-40 WB- AUR reads is very low requires AUR camera investigation - MONITORING

## Number
R&DTASK0001052
## Parent
SDC0003246
## Assignment group
R&D
## Assigned to
Priya Raman

## Priority

4 - Low
## Status

Closed Complete
## Workflow Status

Assigned
## Follow up
## Location
NM Rio Seco I-40 WB

## Description
1.	Detailed description/ Summary of issue
AUR reads is very low 

2.	What is the impact to the customer?
Unable to display company name of the truck

3.	What troubleshooting steps were taken to verify and or resolve issue? 
Loops has been re spliced. 

4.	Recommendation to resolve issue (parts required etc.)
Replace the camera.

## Notes
2026-04-08 Marcus - The image retrieval and bandwidth issue is resolved. Therefore, the R&D task may be closed. However, the AUR Loop is only triggering around 70% of the time, and we cannot rely on the AUR loop for the AUR performance. We disabled the loop in AUR configuration and using the OVC loop as the trigger loop with delays applied.  There should be a separate case task to resolve the AUR loop triggering issue. 

0001713: AUR Network Speed Slowdown Fallback Solution - the issue tracker



2026-03-23 Marcus The below error messages are logged. Increasing the Image Processing Timeout value from 12000ms to 15000 has an effect on the image retrieval. This is verified in the codebase, where the image capturing is interrupted by the if the timeout is flagged. On top of that, I set a task scheduler, which restarts the service if the error occurs, as a temporary patch.



## Close Notes
2026-06-12 Closed as the imaging system has been optimized. Moved to another task for loop investigation

## Activities
Marcus Webb
Image uploaded•2026-03-23 13:56:43

Marcus Webb
Image uploaded•2026-03-23 13:54:49

Diego Herrera
Field changes•2026-03-20 10:32:48
Assigned toPriya Raman
Impact3 - Low
Opened byDiego Herrera
Priority4 - Low
StatusClosed Complete








# NM Clearwater US-60/US-70/US-84 WB- Controller Web Application keeps crashing

## Number
R&DTASK0001019
## Parent
SDC0001739
## Assignment group
R&D
## Assigned to
Marcus Webb

## Priority

3 - Moderate
## Status

Closed Complete
## Workflow Status

Assigned
## Follow up
## Location
NM Clearwater US-60/US-70/US-84 WB

## Description
1.	Detailed description/ Summary of issue
Controller Web Application keeps crashing- No ALPR images 
2.	What is the impact to the customer
Unable to check plates
3.	What troubleshooting steps were taken to verify and or resolve issue 
restarted application and the computer. No go.
4.	Recommendation to resolve issue (parts required etc.) 
Contact HTS.
5.	Was issue resolved
No

## Notes
2025-06-07 mw HTS Support assumes it is a RAM issue. Memory buffer size is reduced to 60MB, and the system needs to be monitored to verify if the change resolved the issue.

2025-06-11 pr No access to site, modem or power issue?

## Close Notes
2025-08-05 mw No crash for >1000 vehicles (since the daily log clear-up). Closing the task.

## Activities
Diego Herrera
Field changes•2025-06-06 14:24:24
Assigned toMarcus Webb
Impact3 - Low
Opened byDiego Herrera
Priority3 - Moderate
StatusClosed Complete









# NM Rio Seco I-40 WB POE-Ramp  ALPR images is not being displayed on the live summary-Inconsistent

## Number
R&DTASK0001034
## Parent
SDC0002439
## Assignment group
R&D
## Assigned to
Marcus Webb

## Priority

3 - Moderate
## Status

Closed Complete
## Workflow Status

Assigned
## Follow up
## Location
NM Rio Seco I-40 WB

## Description
1.	Detailed description/ Summary of issue
Ramp ALPR images is not being displayed on the live summary

2.	What is the impact to the customer?
ALPR matching is broken

3.	What troubleshooting steps were taken to verify and or resolve issue? 
Restarted the Controller web app

4.	Recommendation to resolve issue (parts required etc.)
Check with software

## Notes


## Close Notes
2025-11-13 mw The issue might have been solved by ALPR syntax xml file modification. I could not identify the issue mentioned in this case.

## Activities
Diego Herrera
Field changes•2025-10-14 13:24:30
Assigned toMarcus Webb
Impact3 - Low
Opened byDiego Herrera
Priority3 - Moderate
StatusClosed Complete















# NM Sandia I-40 WB- HTS is offline to test the computer make sure it's compatible with a HTS usb base camera

## Number
R&DTASK0001023
## Parent
SDC0001843
## Assignment group
R&D
## Assigned to
Marcus Webb

## Priority

3 - Moderate
## Status

Closed Complete
## Workflow Status

Assigned
## Follow up
## Location
NM Sandia I-40 WB

## Description
1.	Detailed description/ Summary of issue
HTS computer is offline

2.	What is the impact to the customer?
NO Decode

3.	What troubleshooting steps were taken to verify and or resolve issue? 
Cycle power

4.	Recommendation to resolve issue (parts required etc.)
Site visit

## Notes


## Close Notes
2025-10-09 mw The computer is replaced with a new one.

## Activities
Diego Herrera
Field changes•2025-07-03 15:03:42
Assigned toMarcus Webb
Impact3 - Low
Opened byDiego Herrera
Priority3 - Moderate
StatusClosed Complete









# NM Rio Seco I-40 WB POE- AUR images not being displayed on the live summary

## Number
R&DTASK0001035
## Parent
SDC0002440
## Assignment group
R&D
## Assigned to
Marcus Webb

## Priority

3 - Moderate
## Status

Closed Complete
## Workflow Status

Assigned
## Follow up
## Location
NM Rio Seco I-40 WB

## Description
1.	Detailed description/ Summary of issue
AUR images not being displayed on the live summary

2.	What is the impact to the customer?
NO AUR decode
3.	What troubleshooting steps were taken to verify and or resolve issue? 
Restarted client and cloud AUR. Cycle power the camera. Unable to view the live image on the CamView viewer.

4.	Recommendation to resolve issue (parts required etc.)
Check with R&D

## Notes
2025-10-15 mw The issue is likely caused by the limited bandwidth of the NIC port. CamView Bandwidth Manager shows a maximum of 12 MB/s bandwidth, where it is supposed to be set to 125 MB/s. Increasing the value manually does not work. Limiting transmitting bandwidth to 12MB/s allows for video streaming in CamView Viewer, which was not available when it was set to the default value of 115MB/s, but it results in a coarse temporal resolution of 2.5 fps. The current NIC setting is configured following the Optivue's recommendations.

2025-10-21 mw The ethernet connection was unplugged and plugged back in to the same NIC port. The interface bandwidth increased to 125 Mbps.

2025-11-06 mw Set the max. bandwidth below 90% of the Interface Bandwidth.

2025-11-06 mw Optimized OVC video encoder, recording and VCA settings to mitigate the delayed image retrievals.

## Close Notes
2025-11-07 mw Closing the issue as it has been resolved.

## Activities
Diego Herrera
Field changes•2025-10-14 13:25:39
Assigned toMarcus Webb
Impact3 - Low
Opened byDiego Herrera
Priority3 - Moderate
StatusClosed Complete

























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
