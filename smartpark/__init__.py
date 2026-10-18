"""SmartPark - secure multi-node smart parking system (ECE4810 project).

Architecture (fog computing, Lecture 5a):
    Edge   : 4 x bay nodes (Raspberry Pi + HC-SR04 + traffic-light LEDs)
    Fog    : 1 x gateway (Raspberry Pi + Touch Display 2 kiosk + barrier)
    Cloud  : Telegram (notifications/commands) + ThingSpeak (analytics)
"""

__version__ = "1.0.0"
