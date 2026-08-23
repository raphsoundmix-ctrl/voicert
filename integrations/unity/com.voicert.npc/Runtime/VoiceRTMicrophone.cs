// Optional: stream the player's microphone to the bridge as PCM16 mono 16 kHz.
// The bridge runs VAD on it and fires barge-in when the player talks over
// the NPC. Keep it off if you already have your own push-to-talk / STT.

using UnityEngine;

namespace VoiceRT
{
    [AddComponentMenu("VoiceRT/VoiceRT Microphone")]
    [RequireComponent(typeof(VoiceRTNpc))]
    public sealed class VoiceRTMicrophone : MonoBehaviour
    {
        public string device;                 // null = default device
        public bool streaming = true;
        private const int Rate = 16000;

        private VoiceRTNpc _npc;
        private AudioClip _mic;
        private int _lastPos;
        private float[] _scratch = new float[Rate];
        private byte[] _pcm = new byte[Rate * 2];

        private void OnEnable()
        {
            _npc = GetComponent<VoiceRTNpc>();
            if (Microphone.devices.Length == 0) { enabled = false; return; }
            _mic = Microphone.Start(device, true, 1, Rate);
            _lastPos = 0;
        }

        private void OnDisable()
        {
            if (_mic != null) Microphone.End(device);
            _mic = null;
        }

        private void Update()
        {
            if (!streaming || _mic == null || !_npc.IsConnected) return;
            int pos = Microphone.GetPosition(device);
            if (pos < 0 || pos == _lastPos) return;
            int count = pos > _lastPos ? pos - _lastPos : (_mic.samples - _lastPos) + pos;
            if (count > _scratch.Length) count = _scratch.Length;
            _mic.GetData(_scratch, _lastPos);
            for (int i = 0; i < count; i++)
            {
                short s = (short)Mathf.Clamp(_scratch[i] * 32767f, -32768f, 32767f);
                _pcm[2 * i] = (byte)(s & 0xFF);
                _pcm[2 * i + 1] = (byte)((s >> 8) & 0xFF);
            }
            var chunk = new byte[count * 2];
            System.Buffer.BlockCopy(_pcm, 0, chunk, 0, count * 2);
            _npc.SendMicAudio(chunk);
            _lastPos = pos;
        }
    }
}
