package main

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// android builds the Android app (android/) with its Gradle wrapper and copies the APK
// to <release>/android/agent-proxy-<version>.apk, returning its path there. It's a
// release build when android/keystore.properties exists (see android/README.md), a debug
// build otherwise. Without java or the Android SDK it's skipped with a note ("", nil).
func android(root, release, version string) (string, error) {
	dir := filepath.Join(root, "android")
	if missing := androidMissing(dir); missing != "" {
		fmt.Println("skipping the Android app:", missing)
		return "", nil
	}
	task, variant := "assembleDebug", "debug"
	if _, err := os.Stat(filepath.Join(dir, "keystore.properties")); err == nil {
		task, variant = "assembleRelease", "release"
	}
	wrapper := filepath.Join(dir, "gradlew")
	if runtime.GOOS == "windows" {
		wrapper += ".bat"
	}
	cmd := exec.Command(wrapper, task, "-PappVersion="+version, "--console=plain")
	cmd.Dir = dir
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("gradle %s: %w", task, err)
	}
	built, err := filepath.Glob(filepath.Join(dir, "app", "build", "outputs", "apk", variant, "*.apk"))
	if err != nil || len(built) == 0 {
		return "", fmt.Errorf("gradle %s made no APK", task)
	}
	name := "android/agent-proxy-" + version + ".apk"
	data, err := os.ReadFile(built[0])
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Join(release, "android"), 0o755); err != nil {
		return "", err
	}
	return name, os.WriteFile(filepath.Join(release, filepath.FromSlash(name)), data, 0o644)
}

// androidMissing says what the Android build needs and isn't here, or "".
func androidMissing(dir string) string {
	if _, err := os.Stat(filepath.Join(dir, "settings.gradle.kts")); err != nil {
		return "no android/ project"
	}
	if _, err := exec.LookPath("java"); err != nil && os.Getenv("JAVA_HOME") == "" {
		return "no JDK (java on PATH or JAVA_HOME)"
	}
	if os.Getenv("ANDROID_HOME") != "" || os.Getenv("ANDROID_SDK_ROOT") != "" {
		return ""
	}
	if file, err := os.Open(filepath.Join(dir, "local.properties")); err == nil {
		defer file.Close()
		scanner := bufio.NewScanner(file)
		for scanner.Scan() {
			if strings.HasPrefix(strings.TrimSpace(scanner.Text()), "sdk.dir=") {
				return ""
			}
		}
	}
	return "no Android SDK (ANDROID_HOME, or sdk.dir in android/local.properties)"
}
