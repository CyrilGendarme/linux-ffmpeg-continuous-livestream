#!/usr/bin/env python3
"""
Test script to verify recursive directory scanning works correctly.

This demonstrates that find_audio_files() properly scans multi-level
directory hierarchies and finds audio files at any depth.
"""

import sys
import tempfile
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from media import find_audio_files, SUPPORTED_AUDIO_EXTENSIONS


def test_recursive_scanning():
    """Test that multi-level directories are scanned correctly."""
    
    # Create a temporary directory structure
    with tempfile.TemporaryDirectory() as tmpdir:
        music_dir = Path(tmpdir)
        
        # Create multi-level structure
        level1_album1 = music_dir / "Album 1"
        level1_album1.mkdir()
        (level1_album1 / "song1.mp3").touch()  # Level 1
        
        level2_folder = level1_album1 / "Disc 1"
        level2_folder.mkdir()
        (level2_folder / "song2.flac").touch()  # Level 2
        
        level3_folder = level2_folder / "Bonus"
        level3_folder.mkdir()
        (level3_folder / "song3.m4a").touch()  # Level 3
        
        # Create another branch
        level1_album2 = music_dir / "Album 2"
        level1_album2.mkdir()
        (level1_album2 / "song4.ogg").touch()  # Level 1
        
        level2_folder2 = level1_album2 / "Remixes"
        level2_folder2.mkdir()
        (level2_folder2 / "song5.opus").touch()  # Level 2
        
        # Find all tracks
        found_tracks = find_audio_files(music_dir)
        found_names = sorted([track.name for track in found_tracks])
        
        # Verify all files found
        expected = ["song1.mp3", "song2.flac", "song3.m4a", "song4.ogg", "song5.opus"]
        
        print("=" * 70)
        print("RECURSIVE DIRECTORY SCANNING TEST")
        print("=" * 70)
        print()
        print(f"Test directory: {music_dir}")
        print()
        print("Directory structure created:")
        print("  Album 1/")
        print("    ├── song1.mp3              (Level 1)")
        print("    └── Disc 1/")
        print("        ├── song2.flac        (Level 2)")
        print("        └── Bonus/")
        print("            └── song3.m4a     (Level 3)")
        print("  Album 2/")
        print("    ├── song4.ogg              (Level 1)")
        print("    └── Remixes/")
        print("        └── song5.opus        (Level 2)")
        print()
        print(f"Total files created: {len(expected)}")
        print(f"Total files found:   {len(found_tracks)}")
        print()
        
        if found_names == expected:
            print("✅ SUCCESS: All files found at all depth levels!")
            print()
            print("Files found:")
            for track in found_tracks:
                # Calculate depth
                depth = len(track.relative_to(music_dir).parts) - 1
                indent = "  " * (depth + 1)
                relative_path = track.relative_to(music_dir)
                print(f"{indent}├── {relative_path}")
            return True
        else:
            print("❌ FAILURE: Some files were not found!")
            print(f"Expected: {expected}")
            print(f"Found:    {found_names}")
            return False


def test_format_filtering():
    """Test that only supported formats are found."""
    
    with tempfile.TemporaryDirectory() as tmpdir:
        music_dir = Path(tmpdir)
        
        # Create supported and unsupported files
        (music_dir / "song.mp3").touch()      # ✓ Supported
        (music_dir / "song.flac").touch()     # ✓ Supported
        (music_dir / "song.txt").touch()      # ✗ Unsupported
        (music_dir / "song.pdf").touch()      # ✗ Unsupported
        (music_dir / "song.m4a").touch()      # ✓ Supported
        
        subfolder = music_dir / "subfolder"
        subfolder.mkdir()
        (subfolder / "song.ogg").touch()      # ✓ Supported
        (subfolder / "song.zip").touch()      # ✗ Unsupported
        
        found_tracks = find_audio_files(music_dir)
        found_exts = sorted([track.suffix.lower() for track in found_tracks])
        
        print()
        print("=" * 70)
        print("FORMAT FILTERING TEST")
        print("=" * 70)
        print()
        print("Supported formats:", sorted(SUPPORTED_AUDIO_EXTENSIONS))
        print()
        print("Files created:")
        print("  Root level:")
        print("    ├── song.mp3              ✓ Supported")
        print("    ├── song.flac             ✓ Supported")
        print("    ├── song.txt              ✗ Unsupported")
        print("    ├── song.pdf              ✗ Unsupported")
        print("    └── song.m4a              ✓ Supported")
        print("  Subfolder:")
        print("    ├── song.ogg              ✓ Supported")
        print("    └── song.zip              ✗ Unsupported")
        print()
        print(f"Total files created: 7")
        print(f"Supported files:     4")
        print(f"Files found:         {len(found_tracks)}")
        print()
        
        if len(found_tracks) == 4 and found_exts == [".flac", ".m4a", ".mp3", ".ogg"]:
            print("✅ SUCCESS: Only supported formats found!")
            print()
            print("Files found:")
            for track in found_tracks:
                print(f"  ├── {track.name}")
            return True
        else:
            print("❌ FAILURE: Format filtering not working correctly!")
            print(f"Expected 4 files with exts: .flac, .m4a, .mp3, .ogg")
            print(f"Found {len(found_tracks)} files with exts: {found_exts}")
            return False


def test_empty_directory():
    """Test behavior with empty directory."""
    
    with tempfile.TemporaryDirectory() as tmpdir:
        music_dir = Path(tmpdir)
        
        print()
        print("=" * 70)
        print("EMPTY DIRECTORY TEST")
        print("=" * 70)
        print()
        print(f"Test directory: {music_dir} (empty)")
        print()
        
        try:
            find_audio_files(music_dir)
            print("❌ FAILURE: Should raise FileNotFoundError for empty directory")
            return False
        except FileNotFoundError as e:
            print("✅ SUCCESS: Correctly raises FileNotFoundError")
            print(f"Error message: {e}")
            return True


if __name__ == "__main__":
    print()
    print("╔" + "=" * 68 + "╗")
    print("║" + " " * 68 + "║")
    print("║" + "  RECURSIVE DIRECTORY SCANNING - VERIFICATION TESTS".center(68) + "║")
    print("║" + " " * 68 + "║")
    print("╚" + "=" * 68 + "╝")
    print()
    
    results = []
    
    # Run tests
    results.append(("Recursive Multi-Level Scanning", test_recursive_scanning()))
    results.append(("Format Filtering", test_format_filtering()))
    results.append(("Empty Directory Handling", test_empty_directory()))
    
    # Summary
    print()
    print("=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    print()
    
    all_passed = True
    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {test_name}")
        if not passed:
            all_passed = False
    
    print()
    if all_passed:
        print("╔" + "=" * 68 + "╗")
        print("║" + " " * 68 + "║")
        print("║" + "  ✅ ALL TESTS PASSED - RECURSIVE SCANNING WORKS CORRECTLY".center(68) + "║")
        print("║" + " " * 68 + "║")
        print("╚" + "=" * 68 + "╝")
    else:
        print("╔" + "=" * 68 + "╗")
        print("║" + " " * 68 + "║")
        print("║" + "  ❌ SOME TESTS FAILED - CHECK IMPLEMENTATION".center(68) + "║")
        print("║" + " " * 68 + "║")
        print("╚" + "=" * 68 + "╝")
    
    print()
