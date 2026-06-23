"""
Flipper Application Manifest for AI Agent
Add this to fam_config.py in the root of the ESP32 port project,
or place ai_agent_fam.py inside applications_user/ai_agent/
"""

App(
    appid="ai_agent",
    name="AI Agent",
    apptype=FlipperAppType.EXTERNAL,
    entry_point="ai_agent_app",
    requires=["gui", "storage"],
    stack_size=4 * 1024,
    order=90,
    fap_icon="ai_agent_icon.png",
    fap_category="Tools",
    fap_description="Connect to Claude AI for signal analysis",
    fap_author="Your Name",
    fap_version="1.0",
    sources=["ai_agent_app.c"],
)
