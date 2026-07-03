>>>  119: already_AddRefed<GainNode> GainNode::Create(AudioContext& aAudioContext,
     120:                                             const GainOptions& aOptions,
     121:                                             ErrorResult& aRv) {
     122:   RefPtr<GainNode> audioNode = new GainNode(&aAudioContext);
     123: 
     124:   audioNode->Initialize(aOptions, aRv);
     125:   if (NS_WARN_IF(aRv.Failed())) {
     126:     return nullptr;
     127:   }
     128: 
     129:   audioNode->Gain()->SetInitialValue(aOptions.mGain);
     130:   return audioNode.forget();
     131: }
