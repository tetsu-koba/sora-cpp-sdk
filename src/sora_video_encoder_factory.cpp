#include "sora/sora_video_encoder_factory.h"

// WebRTC
#include <absl/memory/memory.h>
#include <absl/strings/match.h>
#include <api/video_codecs/sdp_video_format.h>
#include <api/video_codecs/video_codec.h>
#include <api/video_codecs/vp9_profile.h>
#include <media/base/codec.h>
#include <media/base/media_constants.h>
#include <media/engine/simulcast_encoder_adapter.h>
#include <modules/video_coding/codecs/h264/include/h264.h>
#include <modules/video_coding/codecs/vp8/include/vp8.h>
#include <modules/video_coding/codecs/vp9/include/vp9.h>
#include <rtc_base/logging.h>

#if !defined(__arm__) || defined(__aarch64__) || defined(__ARM_NEON__)
#include <modules/video_coding/codecs/av1/libaom_av1_encoder.h>
#endif

#if defined(__APPLE__)
#include "sora/mac/mac_video_factory.h"
#endif

#if defined(SORA_CPP_SDK_ANDROID)
#include "sora/android/android_video_factory.h"
#endif

#if USE_NVCODEC_ENCODER
#include "sora/hwenc_nvcodec/nvcodec_h264_encoder.h"
#endif

#if USE_MSDK_ENCODER
#include "sora/hwenc_msdk/msdk_video_encoder.h"
#endif

#if USE_JETSON_ENCODER
#include "sora/hwenc_jetson/jetson_video_encoder.h"
#endif

#include "default_video_formats.h"

namespace sora {

SoraVideoEncoderFactory::SoraVideoEncoderFactory(
    SoraVideoEncoderFactoryConfig config)
    : config_(config) {
  if (config.use_simulcast_adapter) {
    auto config2 = config;
    config2.use_simulcast_adapter = false;
    internal_encoder_factory_.reset(new SoraVideoEncoderFactory(config2));
  }
}

std::vector<webrtc::SdpVideoFormat>
SoraVideoEncoderFactory::GetSupportedFormats() const {
  formats_.clear();

  std::vector<webrtc::SdpVideoFormat> r;
  for (auto& enc : config_.encoders) {
    // factory が定義されてればそれを使う
    // get_supported_formats が定義されてればそれを使う
    // どちらも無ければ codec ごとのデフォルト設定を利用する
    std::vector<webrtc::SdpVideoFormat> formats;
    if (enc.factory != nullptr) {
      formats = enc.factory->GetSupportedFormats();
    } else if (enc.get_supported_formats != nullptr) {
      formats = enc.get_supported_formats();
    } else {
      formats = GetDefaultVideoFormats(enc.codec);
    }
    r.insert(r.end(), formats.begin(), formats.end());
    formats_.push_back(formats);
  }
  return r;
}

std::unique_ptr<webrtc::VideoEncoder>
SoraVideoEncoderFactory::CreateVideoEncoder(
    const webrtc::SdpVideoFormat& format) {
  if (internal_encoder_factory_ != nullptr) {
    return std::unique_ptr<webrtc::VideoEncoder>(
        new webrtc::SimulcastEncoderAdapter(internal_encoder_factory_.get(),
                                            format));
  }

  if (formats_.empty()) {
    GetSupportedFormats();
  }

  webrtc::VideoCodecType specified_codec =
      webrtc::PayloadStringToCodecType(format.name);

  int n = 0;
  for (auto& enc : config_.encoders) {
    // 対応していないフォーマットを CreateVideoEncoder に渡した時の挙動は未定義なので
    // 確実に対応してるフォーマットのみを CreateVideoEncoder に渡すようにする。

    std::function<std::unique_ptr<webrtc::VideoEncoder>(
        const webrtc::SdpVideoFormat&)>
        create_video_encoder;
    std::vector<webrtc::SdpVideoFormat> supported_formats = formats_[n++];

    if (enc.factory != nullptr) {
      create_video_encoder =
          [factory = enc.factory.get()](const webrtc::SdpVideoFormat& format) {
            return factory->CreateVideoEncoder(format);
          };
    } else if (enc.create_video_encoder != nullptr) {
      create_video_encoder = enc.create_video_encoder;
    }

    std::unique_ptr<webrtc::VideoEncoder> r;
    for (const auto& f : supported_formats) {
      if (f.IsSameCodec(format)) {
        return create_video_encoder(format);
      }
    }

    if (r != nullptr) {
      return r;
    }
  }
  return nullptr;
}

SoraVideoEncoderFactoryConfig GetDefaultVideoEncoderFactoryConfig(
    std::shared_ptr<CudaContext> cuda_context,
    void* env) {
  auto config = GetSoftwareOnlyVideoEncoderFactoryConfig();

#if defined(__APPLE__)
  config.encoders.insert(config.encoders.begin(),
                         VideoEncoderConfig(CreateMacVideoEncoderFactory()));
#endif

#if defined(SORA_CPP_SDK_ANDROID)
  if (env != nullptr) {
    config.encoders.insert(config.encoders.begin(),
                           VideoEncoderConfig(CreateAndroidVideoEncoderFactory(
                               static_cast<JNIEnv*>(env))));
  }
#endif

#if USE_NVCODEC_ENCODER
  if (NvCodecH264Encoder::IsSupported(cuda_context)) {
    config.encoders.insert(
        config.encoders.begin(),
        VideoEncoderConfig(webrtc::kVideoCodecH264,
                           [cuda_context = cuda_context](auto format)
                               -> std::unique_ptr<webrtc::VideoEncoder> {
                             return NvCodecH264Encoder::Create(
                                 cricket::VideoCodec(format), cuda_context);
                           }));
  }
#endif

#if USE_MSDK_ENCODER
  auto session = MsdkSession::Create();
  if (MsdkVideoEncoder::IsSupported(session, webrtc::kVideoCodecVP8)) {
    config.encoders.insert(
        config.encoders.begin(),
        VideoEncoderConfig(
            webrtc::kVideoCodecVP8,
            [](auto format) -> std::unique_ptr<webrtc::VideoEncoder> {
              return MsdkVideoEncoder::Create(MsdkSession::Create(),
                                              webrtc::kVideoCodecVP8);
            }));
  }
  if (MsdkVideoEncoder::IsSupported(session, webrtc::kVideoCodecVP9)) {
    config.encoders.insert(
        config.encoders.begin(),
        VideoEncoderConfig(
            webrtc::kVideoCodecVP9,
            [](auto format) -> std::unique_ptr<webrtc::VideoEncoder> {
              return MsdkVideoEncoder::Create(MsdkSession::Create(),
                                              webrtc::kVideoCodecVP9);
            }));
  }
  if (MsdkVideoEncoder::IsSupported(session, webrtc::kVideoCodecH264)) {
    config.encoders.insert(
        config.encoders.begin(),
        VideoEncoderConfig(
            webrtc::kVideoCodecH264,
            [](auto format) -> std::unique_ptr<webrtc::VideoEncoder> {
              return MsdkVideoEncoder::Create(MsdkSession::Create(),
                                              webrtc::kVideoCodecH264);
            }));
  }
  if (MsdkVideoEncoder::IsSupported(session, webrtc::kVideoCodecAV1)) {
    config.encoders.insert(
        config.encoders.begin(),
        VideoEncoderConfig(
            webrtc::kVideoCodecAV1,
            [](auto format) -> std::unique_ptr<webrtc::VideoEncoder> {
              return MsdkVideoEncoder::Create(MsdkSession::Create(),
                                              webrtc::kVideoCodecAV1);
            }));
  }
#endif

#if USE_JETSON_ENCODER
  if (JetsonVideoEncoder::IsSupportedVP8()) {
    config.encoders.insert(
        config.encoders.begin(),
        VideoEncoderConfig(webrtc::kVideoCodecVP8, [](auto format) {
          return std::unique_ptr<webrtc::VideoEncoder>(
              absl::make_unique<JetsonVideoEncoder>(
                  cricket::VideoCodec(format)));
        }));
  }
  if (JetsonVideoEncoder::IsSupportedVP9()) {
    config.encoders.insert(
        config.encoders.begin(),
        VideoEncoderConfig(webrtc::kVideoCodecVP9, [](auto format) {
          return std::unique_ptr<webrtc::VideoEncoder>(
              absl::make_unique<JetsonVideoEncoder>(
                  cricket::VideoCodec(format)));
        }));
  }
  if (JetsonVideoEncoder::IsSupportedAV1()) {
    config.encoders.insert(
        config.encoders.begin(),
        VideoEncoderConfig(webrtc::kVideoCodecAV1, [](auto format) {
          return std::unique_ptr<webrtc::VideoEncoder>(
              absl::make_unique<JetsonVideoEncoder>(
                  cricket::VideoCodec(format)));
        }));
  }
  config.encoders.insert(
      config.encoders.begin(),
      VideoEncoderConfig(webrtc::kVideoCodecH264, [](auto format) {
        return std::unique_ptr<webrtc::VideoEncoder>(
            absl::make_unique<JetsonVideoEncoder>(cricket::VideoCodec(format)));
      }));
#endif

  return config;
}

SoraVideoEncoderFactoryConfig GetSoftwareOnlyVideoEncoderFactoryConfig() {
  SoraVideoEncoderFactoryConfig config;
  config.encoders.push_back(VideoEncoderConfig(
      webrtc::kVideoCodecVP8,
      [](auto format) { return webrtc::VP8Encoder::Create(); }));
  config.encoders.push_back(
      VideoEncoderConfig(webrtc::kVideoCodecVP9, [](auto format) {
        return webrtc::VP9Encoder::Create(cricket::VideoCodec(format));
      }));
#if !defined(__arm__) || defined(__aarch64__) || defined(__ARM_NEON__)
  config.encoders.push_back(VideoEncoderConfig(
      webrtc::kVideoCodecAV1,
      [](auto format) { return webrtc::CreateLibaomAv1Encoder(); }));
#endif
  return config;
}

}  // namespace sora
