#!/usr/bin/env python3
"""
Test suite for multiple background videos feature.

This tests:
1. Configuration parsing
2. Backward compatibility
3. File validation
4. Error handling
"""

import sys
from pathlib import Path

def test_config_parsing():
    """Test background_videos configuration parsing."""
    from livestream.config import _parse_background_videos
    
    print("[TEST] Config parsing...")
    
    # Test multiple videos
    result = _parse_background_videos('video1.mp4, video2.mp4, video3.mp4')
    assert result == ['video1.mp4', 'video2.mp4', 'video3.mp4'], f"Multiple parse failed: {result}"
    
    # Test single video
    result = _parse_background_videos('single.mp4')
    assert result == ['single.mp4'], f"Single parse failed: {result}"
    
    # Test None
    result = _parse_background_videos(None)
    assert result == [], f"None parse failed: {result}"
    
    # Test empty string
    result = _parse_background_videos('')
    assert result == [], f"Empty parse failed: {result}"
    
    # Test with spaces
    result = _parse_background_videos('  video1.mp4  ,  video2.mp4  ')
    assert result == ['video1.mp4', 'video2.mp4'], f"Whitespace parse failed: {result}"
    
    print("  [PASS] Config parsing works correctly")


def test_config_validation():
    """Test configuration validation and backward compatibility."""
    from livestream.config import validate_config
    
    print("[TEST] Config validation...")
    
    # Test multiple background_videos
    config = {
        'music_dir': '/tmp/music',
        'background_videos': 'vid1.mp4, vid2.mp4',
        'rtmp_url': 'rtmp://test'
    }
    clean, errors = validate_config(config, check_paths=False)
    assert not errors, f"Should have no errors, got: {errors}"
    assert clean['background_videos'] == 'vid1.mp4, vid2.mp4'
    
    # Test backward compat - only background_video
    config = {
        'music_dir': '/tmp/music',
        'background_video': 'vid.mp4',
        'rtmp_url': 'rtmp://test'
    }
    clean, errors = validate_config(config, check_paths=False)
    assert not errors, f"Backward compat should work, got errors: {errors}"
    assert clean.get('background_videos') == 'vid.mp4', \
        f"Should convert background_video to background_videos"
    
    # Test both set (new takes precedence)
    config = {
        'music_dir': '/tmp/music',
        'background_video': 'old.mp4',
        'background_videos': 'new1.mp4, new2.mp4',
        'rtmp_url': 'rtmp://test'
    }
    clean, errors = validate_config(config, check_paths=False)
    assert not errors, f"Both set should work, got errors: {errors}"
    assert clean['background_videos'] == 'new1.mp4, new2.mp4', \
        f"New should take precedence"
    
    print("  [PASS] Config validation and backward compat works")


def test_background_manager_errors():
    """Test BackgroundManager error handling."""
    from livestream.background_manager import prepare_backgrounds
    
    print("[TEST] Error handling...")
    
    class MockArgv:
        cache_dir = '/tmp/cache'
        video_fps = 30.0
        background_bitrate = '4M'
        background_size = '1280x720'
        background_videos = None
        background_video = None
    
    argv = MockArgv()
    
    # Should raise when no videos configured
    try:
        prepare_backgrounds(argv)
        assert False, "Should have raised ValueError for no videos"
    except ValueError as e:
        assert "No background videos configured" in str(e)
    
    # Should raise when video not found
    argv.background_video = '/nonexistent/video.mp4'
    try:
        prepare_backgrounds(argv)
        assert False, "Should have raised FileNotFoundError"
    except FileNotFoundError as e:
        assert "Background video not found" in str(e)
    
    print("  [PASS] Error handling works correctly")


def test_imports():
    """Test that all imports work."""
    print("[TEST] Module imports...")
    
    try:
        from livestream.config import _parse_background_videos, validate_config
        from livestream.background_manager import prepare_backgrounds
        from livestream.runner import prepare_background, prepare_backgrounds as pb
        print("  [PASS] All imports successful")
    except ImportError as e:
        print(f"  [FAIL] Import error: {e}")
        return False
    
    return True


def main():
    """Run all tests."""
    print("=" * 60)
    print("TESTING: Multiple Background Videos Feature")
    print("=" * 60)
    print()
    
    all_pass = True
    
    try:
        if not test_imports():
            all_pass = False
        
        test_config_parsing()
        test_config_validation()
        test_background_manager_errors()
        
    except Exception as e:
        print(f"\n[FAIL] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        all_pass = False
    
    print()
    print("=" * 60)
    if all_pass:
        print("STATUS: ALL TESTS PASSED")
    else:
        print("STATUS: SOME TESTS FAILED")
    print("=" * 60)
    
    return 0 if all_pass else 1


if __name__ == '__main__':
    sys.exit(main())
