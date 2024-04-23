# lab-in-a-box



![Image of one of the NUCs i used to test and develop this lab.](media/NUC.jpg)

The purpose of this project is to enable users to setup their own lab where they can quickly try different software and its features in a reliable and controlled manner with modularity and automation in mind.
It contains a set of scripts that should make it easy to have your own lab and try different software.

This repository containing instructions and scripts to setup a lab in a NUC (or whatever hardware) from scratch.


In the words of an AI:

"Easily set up your own lab to try out various software hassle-free. Our scripts and instructions ensure a smooth, controlled experience, promoting modularity and automation. Get experimenting!"




## Quick Start

- **Scenario A)** Dedicated hardware and OS.
  
 1. The first step will be to prepare the media to install the OS, we have choosen SLES as the preferred OS, this is what we will need:
   
    - An empty USB of at least 8GB
    - Download an ISO image for SLES ( https://www.suse.com/download/sles/ ie. SLE-15-SP5-Online-x86_64-GM-Media1.iso )

    Once you have downloaded the ISO image you can follow the instructions on ( https://www.suse.com/support/kb/doc/?id=000018742 ) or do this steps:
   - Without the usb you want to use, run the following command:

```shell
     cat /proc/partitions  >/tmp/partb4
```
     
   - Now connect the usb and run:

```shell
     cat /proc/partitions  >/tmp/parta3
```
     
   - Now lets see what's the usb device name:

```shell
     $ diff /tmp/part*
     27,29d26
     <    8       32   15649792 sdZ
     <    8       33       3654 sdZ1
     <    8       34     501008 sdZ2
```
    
  - In this case we will to use /dev/**sdZ**, so let's write the iso to the usb:

      **IMPORTANT: The contents of the USB will be lost forever, make sure you don't have anything valuable.**

```shell
     dd if=SLE-15-SP5-Online-x86_64-GM-Media1.iso of=/dev/sdZ bs=4k && sync
```
     
   - After this we can *remove the USB* drive and *boot your lab node with it* to proceed with the install.


 2. Boot and install SLES from the USB media



