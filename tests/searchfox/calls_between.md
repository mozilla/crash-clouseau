# calls-between-source:'mozilla::dom::AudioContext' calls-between-target:'mozilla::dom::GainNode' depth:3

## Direct calls from source to target

- **mozilla::dom::AudioContext::CollectReports** (dom/media/webaudio/AudioContext.cpp#1342) calls **mozilla::dom::AudioNode::SizeOfIncludingThis** (dom/media/webaudio/AudioNode.cpp#118)
  - From: `_ZN7mozilla3dom12AudioContext14CollectReportsEP23nsIHandleReportCallbackP11nsISupportsb`
  - To: `_ZNK7mozilla3dom9AudioNode19SizeOfIncludingThisEPFyPKvE`
- **mozilla::dom::AudioNode::SizeOfIncludingThis** (dom/media/webaudio/AudioNode.cpp#118) calls **mozilla::dom::GainNode::SizeOfIncludingThis** (dom/media/webaudio/GainNode.cpp#139)
  - From: `_ZNK7mozilla3dom9AudioNode19SizeOfIncludingThisEPFyPKvE`
  - To: `_ZNK7mozilla3dom8GainNode19SizeOfIncludingThisEPFyPKvE`
- **mozilla::dom::AudioContext::CreateGain** (dom/media/webaudio/AudioContext.cpp#474) calls **mozilla::dom::GainNode::Create** (dom/media/webaudio/GainNode.cpp#119)
  - From: `_ZN7mozilla3dom12AudioContext10CreateGainERNS_11ErrorResultE`
  - To: `_ZN7mozilla3dom8GainNode6CreateERNS0_12AudioContextERKNS0_11GainOptionsERNS_11ErrorResultE`
