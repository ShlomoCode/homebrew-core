class AwsSdkCpp < Formula
  desc "AWS SDK for C++"
  homepage "https://github.com/aws/aws-sdk-cpp"
  url "https://github.com/aws/aws-sdk-cpp/archive/refs/tags/1.11.900.tar.gz"
  sha256 "35a895edbcf174ed8587859af62fcca85868e82034d019a5e584e27893dc7909"
  license "Apache-2.0"
  revision 1
  compatibility_version 3
  head "https://github.com/aws/aws-sdk-cpp.git", branch: "main"

  livecheck do
    throttle 15
  end

  bottle do
    sha256 cellar: :any, arm64_golden_gate: "5b95cbd5850e5e094ec39bcb554d2a71814192859ed1084fd1cf4bccb43ffdc4"
    sha256 cellar: :any, arm64_tahoe:       "effa2670978eeff7b770a4a2c409bc7a97f8663fe666f90e8e78ef340a6fed58"
    sha256 cellar: :any, arm64_sequoia:     "245d4b7d08b3f8cc01df49a54a1c013db6d2374bd6c234d7ec28482f026a733a"
    sha256 cellar: :any, arm64_linux:       "5e8d4c91ec9293163d6ff776b3ce49eb8b7ebcfd3ff02171f94c4e194b309048"
    sha256 cellar: :any, x86_64_linux:      "3c2eebf2c40ed53dffcfe666e4b99132a084aa78a7d9030478e5c451d3c90702"
  end

  depends_on "cmake" => :build
  depends_on "aws-c-auth"
  depends_on "aws-c-common"
  depends_on "aws-c-event-stream"
  depends_on "aws-c-http"
  depends_on "aws-c-io"
  depends_on "aws-c-s3"
  depends_on "aws-crt-cpp"

  uses_from_macos "curl"

  on_linux do
    depends_on "zlib-ng-compat"
  end

  def install
    # Avoid OOM failure on Github runner
    ENV.deparallelize if OS.linux? && ENV["HOMEBREW_GITHUB_ACTIONS"].present?

    linker_flags = ["-Wl,-rpath,#{rpath}"]
    # Avoid overlinking to aws-c-* indirect dependencies
    linker_flags << "-Wl,-dead_strip_dylibs" if OS.mac?

    args = %W[
      -DBUILD_DEPS=OFF
      -DCMAKE_MODULE_PATH=#{formula_opt_lib("aws-c-common")}/cmake/aws-c-common/modules
      -DCMAKE_SHARED_LINKER_FLAGS=#{linker_flags.join(" ")}
      -DENABLE_TESTING=OFF
    ]

    system "cmake", "-S", ".", "-B", "build", *args, *std_cmake_args
    system "cmake", "--build", "build"
    system "cmake", "--install", "build"
  end

  test do
    (testpath/"test.cpp").write <<~CPP
      #include <aws/core/Version.h>
      #include <iostream>

      int main() {
          std::cout << Aws::Version::GetVersionString() << std::endl;
          return 0;
      }
    CPP
    system ENV.cxx, "-std=c++11", "test.cpp", "-L#{lib}", "-laws-cpp-sdk-core", "-o", "test"
    system "./test"
  end
end
