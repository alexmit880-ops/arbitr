"""Запуск всех тестов с отчётом."""
import subprocess
import sys
import os


def run_tests():
    """Запустить pytest с подробным выводом."""
    
    test_files = [
        "tests/test_models.py",
        "tests/test_profit_calculator.py",
        "tests/test_zombie_detector.py",
        "tests/test_risk_manager.py",
        "tests/test_kelly_sizing.py",
        "tests/test_confidence_scorer.py",
    ]
    
    print("=" * 70)
    print("🧪 ARBITRAGE SCANNER — TEST SUITE")
    print("=" * 70)
    
    all_passed = True
    results = {}
    
    for test_file in test_files:
        if not os.path.exists(test_file):
            print(f"❌ File not found: {test_file}")
            all_passed = False
            continue
        
        print(f"\n📁 Running {test_file}...")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", test_file, "-v", "--tb=short"],
            capture_output=True,
            text=True,
        )
        
        # Парсим результат
        output = result.stdout + result.stderr
        
        # Считаем passed/failed
        passed = output.count(" PASSED")
        failed = output.count(" FAILED")
        errors = output.count(" ERROR")
        
        results[test_file] = {"passed": passed, "failed": failed, "errors": errors}
        
        status = "✅" if result.returncode == 0 else "❌"
        print(f"{status} {test_file}: {passed} passed, {failed} failed, {errors} errors")
        
        if result.returncode != 0:
            all_passed = False
            # Показать детали ошибок
            lines = output.split("\n")
            for line in lines:
                if "FAILED" in line or "Error" in line or "assert" in line:
                    print(f"   {line.strip()}")
    
    # Итоговый отчёт
    print("\n" + "=" * 70)
    print("📊 SUMMARY")
    print("=" * 70)
    
    total_passed = sum(r["passed"] for r in results.values())
    total_failed = sum(r["failed"] for r in results.values())
    total_errors = sum(r["errors"] for r in results.values())
    
    print(f"  Total passed:  {total_passed}")
    print(f"  Total failed:  {total_failed}")
    print(f"  Total errors:  {total_errors}")
    
    if all_passed and total_failed == 0 and total_errors == 0:
        print("\n  🎉 ALL TESTS PASSED!")
        return 0
    else:
        print("\n  ⚠️  SOME TESTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(run_tests())
