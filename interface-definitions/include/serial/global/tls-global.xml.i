<!-- include start from serial/global/tls-global.xml.i -->
<node name="tls">
  <properties>
    <help>TLS setting</help>
  </properties>
  <children>
    <tagNode name="template">
      <properties>
        <help>TLS template name</help>
      </properties>
      <children>
        <leafNode name="certificate">
          <properties>
            <help>Certificate</help>
            <completionHelp>
              <script>${vyos_completion_dir}/list_pki_with_tpm.py --selector cert</script>
            </completionHelp>
            <valueHelp>
              <format>txt</format>
              <description>Certificate name</description>
            </valueHelp>
          </properties>
        </leafNode>
        <leafNode name="passphrase">
          <properties>
            <help>Private key passphrase</help>
            <constraint>
              <regex>.{0,16}</regex>
            </constraint>
            <valueHelp>
              <format>txt</format>
              <description>Passphrase to decrypt the private key</description>
            </valueHelp>
          </properties>
        </leafNode>
        <leafNode name="version">
          <properties>
            <help>TLS version</help>
            <completionHelp>
              <list>any tlsv1.2 tlsv1.2b tlsv1.3</list>
            </completionHelp>
            <valueHelp>
              <format>any</format>
              <description>Any TLS version</description>
            </valueHelp>
            <valueHelp>
              <format>tlsv1.2</format>
              <description>TLS version 1.2</description>
            </valueHelp>
            <valueHelp>
              <format>tlsv1.2b</format>
              <description>TLS version 1.2 Suite B</description>
            </valueHelp>
            <valueHelp>
              <format>tlsv1.3</format>
              <description>TLS version 1.3</description>
            </valueHelp>
            <constraint>
              <regex>(any|tlsv1.2|tlsv1.2b|tlsv1.3)</regex>
            </constraint>
          </properties>
          <defaultValue>any</defaultValue>
        </leafNode>
        <leafNode name="role">
          <properties>
            <help>TLS role</help>
            <completionHelp>
              <list>client server</list>
            </completionHelp>
            <valueHelp>
              <format>client</format>
              <description>TLS client role</description>
            </valueHelp>
            <valueHelp>
              <format>server</format>
              <description>TLS server role</description>
            </valueHelp>
            <constraint>
              <regex>(client|server)</regex>
            </constraint>
          </properties>
          <defaultValue>client</defaultValue>
        </leafNode>
        <node name="peer-verification">
          <properties>
            <help>Verification of peer certificate</help>
          </properties>
          <children>
            <leafNode name="country">
              <properties>
                <help>Country</help>
                <valueHelp>
                  <format>txt</format>
                  <description>Country</description>
                </valueHelp>
              </properties>
            </leafNode>
            <leafNode name="state">
              <properties>
                <help>state</help>
                <valueHelp>
                  <format>txt</format>
                  <description>state</description>
                </valueHelp>
              </properties>
            </leafNode>
            <leafNode name="locality">
              <properties>
                <help>locality</help>
                <valueHelp>
                  <format>txt</format>
                  <description>locality</description>
                </valueHelp>
              </properties>
            </leafNode>
            <leafNode name="organization">
              <properties>
                <help>organization</help>
                <valueHelp>
                  <format>txt</format>
                  <description>organization</description>
                </valueHelp>
              </properties>
            </leafNode>
            <leafNode name="organization-unit">
              <properties>
                <help>organization unit</help>
                <valueHelp>
                  <format>txt</format>
                  <description>organization unit</description>
                </valueHelp>
              </properties>
            </leafNode>
            <leafNode name="common-name">
              <properties>
                <help>common name</help>
                <valueHelp>
                  <format>txt</format>
                  <description>common name</description>
                </valueHelp>
              </properties>
            </leafNode>
            <leafNode name="email">
              <properties>
                <help>email</help>
                <valueHelp>
                  <format>txt</format>
                  <description>email</description>
                </valueHelp>
              </properties>
            </leafNode>
          </children>
        </node>
        <tagNode name="cipher-options">
          <properties>
            <help>Cipher option</help>
            <valueHelp>
              <format>u32:1-5</format>
              <description>Cipher (1-5)</description>
            </valueHelp>
            <constraint>
              <validator name="numeric" argument="--range 1-5"/>
            </constraint>
          </properties>
          <children>
            <leafNode name="encryption">
              <properties>
                <help>Cipher encryption</help>
                <completionHelp>
                  <list>any aes aes-gcm</list>
                </completionHelp>
                <valueHelp>
                  <format>any</format>
                  <description>Any encryption algorithm</description>
                </valueHelp>
                <valueHelp>
                  <format>aes</format>
                  <description>AES encryption</description>
                </valueHelp>
                <valueHelp>
                  <format>aes-gcm</format>
                  <description>AES-GCM encryption</description>
                </valueHelp>
                <constraint>
                  <regex>(any|aes|aes-gcm)</regex>
                </constraint>
              </properties>
              <defaultValue>any</defaultValue>
            </leafNode>
            <leafNode name="min-key-size">
              <properties>
                <help>Cipher min key size</help>
                <completionHelp>
                  <list>40 56 64 128 168 256</list>
                </completionHelp>
                <valueHelp>
                  <format>40</format>
                  <description>40-bit key size</description>
                </valueHelp>
                <valueHelp>
                  <format>56</format>
                  <description>56-bit key size</description>
                </valueHelp>
                <valueHelp>
                  <format>64</format>
                  <description>64-bit key size</description>
                </valueHelp>
                <valueHelp>
                  <format>128</format>
                  <description>128-bit key size</description>
                </valueHelp>
                <valueHelp>
                  <format>168</format>
                  <description>168-bit key size</description>
                </valueHelp>
                <valueHelp>
                  <format>256</format>
                  <description>256-bit key size</description>
                </valueHelp>
                <constraint>
                  <regex>(40|56|64|128|168|256)</regex>
                </constraint>
              </properties>
              <defaultValue>40</defaultValue>
            </leafNode>
            <leafNode name="max-key-size">
              <properties>
                <help>Cipher max key size</help>
                <completionHelp>
                  <list>40 56 64 128 168 256</list>
                </completionHelp>
                <valueHelp>
                  <format>40</format>
                  <description>40-bit key size</description>
                </valueHelp>
                <valueHelp>
                  <format>56</format>
                  <description>56-bit key size</description>
                </valueHelp>
                <valueHelp>
                  <format>64</format>
                  <description>64-bit key size</description>
                </valueHelp>
                <valueHelp>
                  <format>128</format>
                  <description>128-bit key size</description>
                </valueHelp>
                <valueHelp>
                  <format>168</format>
                  <description>168-bit key size</description>
                </valueHelp>
                <valueHelp>
                  <format>256</format>
                  <description>256-bit key size</description>
                </valueHelp>
                <constraint>
                  <regex>(40|56|64|128|168|256)</regex>
                </constraint>
              </properties>
              <defaultValue>256</defaultValue>
            </leafNode>
            <leafNode name="key-exchange">
              <properties>
                <help>Cipher key exchange</help>
                <completionHelp>
                  <list>any rsa edh-rsa edh-dss adh ecdh-ecdsa</list>
                </completionHelp>
                <valueHelp>
                  <format>any</format>
                  <description>Any key exchange algorithm</description>
                </valueHelp>
                <valueHelp>
                  <format>rsa</format>
                  <description>RSA key exchange</description>
                </valueHelp>
                <valueHelp>
                  <format>edh-rsa</format>
                  <description>Ephemeral Diffie-Hellman with RSA</description>
                </valueHelp>
                <valueHelp>
                  <format>edh-dss</format>
                  <description>Ephemeral Diffie-Hellman with DSS</description>
                </valueHelp>
                <valueHelp>
                  <format>adh</format>
                  <description>Anonymous Diffie-Hellman</description>
                </valueHelp>
                <valueHelp>
                  <format>ecdh-ecdsa</format>
                  <description>Elliptic Curve Diffie-Hellman with ECDSA</description>
                </valueHelp>
                <constraint>
                  <regex>(any|rsa|edh-rsa|edh-dss|adh|ecdh-ecdsa)</regex>
                </constraint>
              </properties>
              <defaultValue>any</defaultValue>
            </leafNode>
            <leafNode name="hmac">
              <properties>
                <help>Cipher hash message authentication code</help>
                <completionHelp>
                  <list>any sha1 md5 sha256 sha384</list>
                </completionHelp>
                <valueHelp>
                  <format>any</format>
                  <description>Any HMAC algorithm</description>
                </valueHelp>
                <valueHelp>
                  <format>sha1</format>
                  <description>SHA-1 HMAC</description>
                </valueHelp>
                <valueHelp>
                  <format>md5</format>
                  <description>MD5 HMAC</description>
                </valueHelp>
                <valueHelp>
                  <format>sha256</format>
                  <description>SHA-256 HMAC</description>
                </valueHelp>
                <valueHelp>
                  <format>sha384</format>
                  <description>SHA-384 HMAC</description>
                </valueHelp>
                <constraint>
                  <regex>(any|sha1|md5|sha256|sha384)</regex>
                </constraint>
              </properties>
              <defaultValue>any</defaultValue>
            </leafNode>
          </children>
        </tagNode>
      </children>
    </tagNode>
  </children>
</node>
<!-- include end -->
